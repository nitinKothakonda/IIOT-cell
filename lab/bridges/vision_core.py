"""vision_core.py - PROVIDED. Do not modify.

Camera, colour classification and the MJPEG stream for the inspection
station, already tuned and tested on the real cell.

    from vision_core import Vision
    vis = Vision(sim=False)        # opens the camera, starts the stream
    result = vis.inspect()         # 1 pass (blue), 2 reject (green), 3 no tape
    vis.new_wafer()                # clear the verdict drawn on the stream
    vis.close()

inspect() takes 5 fresh frames, classifies each with HSV thresholds inside
the region of interest (ROI) and returns the majority vote. It knows
nothing about the PLC - that is your job.

Live stream for the dashboard (real camera only):
    http://<laptop-ip>:8080/stream        MJPEG, ROI box drawn on it
    http://<laptop-ip>:8080/snapshot.jpg  single frame

Instructor tools:
    python vision_core.py --cams      list the cameras OpenCV can open
    python vision_core.py --setup     live view to tune the ROI and thresholds

With sim=True no camera is used. inspect() reads the simulator-only
register 400100 (tape colour of the wafer at the pick point) from
plc_sim.py instead. That register does not exist on the real PLC.

WHY THREE RESULTS AND NOT TAPE-OR-NOTHING
If "reject" were simply the absence of tape, an unplugged, dark or
mis-aimed camera would pass every wafer. Detecting both colours
positively means a broken camera gives a third answer (3) that the PLC
treats as a fault.
"""

import sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import cv2
    import numpy as np
except ImportError:          # fine in sim mode
    cv2 = np = None

CAM_INDEX = 1

# MJPEG stream for the dashboard
STREAM_PORT  = 8080
STREAM_FPS   = 15      # plenty for a dashboard, keeps CPU down
JPEG_QUALITY = 70

# Backends tried in order. DSHOW opens many cameras but hands back black
# frames on some; MSMF usually works where DSHOW does not. ANY lets
# OpenCV choose. --cams shows which of these actually delivers an image.
BACKENDS = ([("DSHOW", cv2.CAP_DSHOW),
             ("MSMF", cv2.CAP_MSMF),
             ("ANY", cv2.CAP_ANY)] if cv2 else [])

# ROI over the disk at the pick point: x, y, width, height in pixels.
# Set this in --setup mode.
ROI = [215, 210, 245, 225]

# HSV ranges. OpenCV hue is 0-179, not 0-359.
GREEN = [((78, 90, 120), (95, 255, 255))]
BLUE  = [((97, 120, 120), (125, 255, 255))]
RED   = [((0, 80, 60), (10, 255, 255)),        # red wraps the hue circle,
         ((170, 80, 60), (179, 255, 255))]     # so it needs two ranges

PASS_RANGES   = BLUE       # blue tape  = good disk
REJECT_RANGES = GREEN      # green tape = bad disk

MIN_PIXELS = 4000       # how much tape must be visible to count
SAMPLES    = 5         # frames averaged per inspection

# overlay colours (BGR) and labels per result
RESULT_STYLE = {1: ((255, 0, 0), "PASS (blue)"),
                2: ((0, 200, 0), "REJECT (green)"),
                3: ((0, 0, 255), "NO TAPE")}


# ---------------------------------------------------------------- camera

def try_open(index, flag, name):
    """Open and actually pull a frame. isOpened() alone lies."""
    cap = cv2.VideoCapture(index, flag)
    if not cap.isOpened():
        cap.release()
        return None, f"{name}: would not open"
    for _ in range(10):                 # some cameras start black
        cap.read()
        time.sleep(0.05)
    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        return None, f"{name}: opened but no frame"
    if frame.max() < 10:
        cap.release()
        return None, f"{name}: frames are black"
    return cap, f"{name}: ok, {frame.shape[1]}x{frame.shape[0]}"


def open_camera(index=None):
    index = CAM_INDEX if index is None else index
    problems = []
    for name, flag in BACKENDS:
        cap, msg = try_open(index, flag, name)
        print(f"  {msg}")
        if cap:
            return cap
        problems.append(msg)
    raise RuntimeError("no backend delivered an image from camera "
                       f"{index}:\n    " + "\n    ".join(problems))


def list_cams():
    print("checking camera indexes 0-3 on each backend\n")
    for i in range(4):
        print(f"index {i}:")
        found = False
        for name, flag in BACKENDS:
            cap, msg = try_open(i, flag, name)
            print(f"  {msg}")
            if cap:
                cap.release()
                found = True
        if not found:
            print("  nothing usable")
        print()
    print("set CAM_INDEX to whichever index reported ok")


class FrameSource:
    """Sole owner of the camera in bridge mode.

    A background thread reads continuously and keeps the newest frame.
    Everyone else asks for frames here instead of touching the camera.
    """

    def __init__(self, cap):
        self.cap = cap
        self.cond = threading.Condition()
        self.frame = None
        self.count = 0              # increments on every new frame
        self.last_result = None     # (res, pass_px, reject_px) for the overlay
        self.alive = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while self.alive:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            with self.cond:
                self.frame = frame
                self.count += 1
                self.cond.notify_all()

    def latest(self):
        with self.cond:
            return self.frame, self.count

    def next_frame(self, after, timeout=1.0):
        """Block until a frame newer than `after` exists.
        Returns (frame, count), or (None, after) on timeout."""
        with self.cond:
            self.cond.wait_for(lambda: self.count > after, timeout=timeout)
            if self.count > after:
                return self.frame, self.count
            return None, after

    def stop(self):
        self.alive = False
        self.thread.join(timeout=1.0)


# ------------------------------------------------------------ classify

def mask_for(hsv, ranges):
    m = None
    for lo, hi in ranges:
        part = cv2.inRange(hsv, np.array(lo), np.array(hi))
        m = part if m is None else cv2.bitwise_or(m, part)
    # knock out speckle from glare on the glass
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    return m


def classify(frame):
    """Returns (result, pass_px, reject_px)."""
    x, y, w, h = ROI
    roi = frame[y:y + h, x:x + w]
    if roi.size == 0:
        return 3, 0, 0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    p = int(cv2.countNonZero(mask_for(hsv, PASS_RANGES)))
    r = int(cv2.countNonZero(mask_for(hsv, REJECT_RANGES)))

    if p < MIN_PIXELS and r < MIN_PIXELS:
        return 3, p, r                  # neither - camera or disk problem
    return (1, p, r) if p > r else (2, p, r)


# --------------------------------------------------------------- setup

def setup():
    cap = open_camera()
    print(__doc__)
    print("  WASD  move ROI      IJKL  resize ROI")
    print("  p     print the ROI and hue stats")
    print("  q     quit\n")

    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        x, y, w, h = ROI
        res, p, r = classify(frame)

        vis = frame.copy()
        colour, label = RESULT_STYLE[res]
        cv2.rectangle(vis, (x, y), (x + w, y + h), colour, 2)
        cv2.putText(vis, f"{label}  pass={p} reject={r}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2)
        cv2.putText(vis, f"ROI {ROI}", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.imshow("vision setup", vis)

        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break
        elif k == ord("a"):  ROI[0] -= 5
        elif k == ord("d"):  ROI[0] += 5
        elif k == ord("w"):  ROI[1] -= 5
        elif k == ord("s"):  ROI[1] += 5
        elif k == ord("j"):  ROI[2] = max(20, ROI[2] - 5)
        elif k == ord("l"):  ROI[2] += 5
        elif k == ord("i"):  ROI[3] = max(20, ROI[3] - 5)
        elif k == ord("k"):  ROI[3] += 5
        elif k == ord("p"):
            roi = frame[y:y + h, x:x + w]
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            print(f"\n  ROI = {ROI}")
            print(f"  hue median {np.median(hsv[:,:,0]):6.1f}  "
                  f"range {hsv[:,:,0].min():3d}-{hsv[:,:,0].max():3d}")
            print(f"  sat median {np.median(hsv[:,:,1]):6.1f}")
            print(f"  val median {np.median(hsv[:,:,2]):6.1f}")
            print(f"  pass px {p}   reject px {r}   -> {label}")

    cap.release()
    cv2.destroyAllWindows()
    print(f"\nput this back in the file:\n    ROI = {ROI}")


# -------------------------------------------------------------- stream

def annotate(frame, last):
    """ROI box on the frame; coloured by the last verdict, white if none."""
    vis = frame.copy()
    x, y, w, h = ROI
    colour = (255, 255, 255) if last is None else RESULT_STYLE[last[0]][0]
    cv2.rectangle(vis, (x, y), (x + w, y + h), colour, 2)
    cv2.putText(vis, f"ROI {x},{y}  {w}x{h}", (x, max(15, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
    return vis


def encode(img):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes() if ok else None


def make_handler(src):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # keep the console readable
            pass

        def do_GET(self):
            if self.path.startswith("/stream"):
                self._stream()
            elif self.path.startswith("/snapshot"):
                self._snapshot()
            else:
                self.send_error(404, "use /stream or /snapshot.jpg")

        def _snapshot(self):
            frame, _ = src.latest()
            jpg = encode(annotate(frame, src.last_result)) if frame is not None else None
            if jpg is None:
                self.send_error(503, "no frame yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(jpg)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            n = 0
            period = 1.0 / STREAM_FPS
            try:
                while True:
                    frame, n = src.next_frame(n)
                    if frame is None:
                        continue                 # camera stalled; keep waiting
                    jpg = encode(annotate(frame, src.last_result))
                    if jpg is None:
                        continue
                    self.wfile.write(b"--frame\r\n"
                                     b"Content-Type: image/jpeg\r\n"
                                     b"Content-Length: " + str(len(jpg)).encode() +
                                     b"\r\n\r\n" + jpg + b"\r\n")
                    time.sleep(period)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return                           # viewer closed the page

    return Handler


def start_stream(src):
    server = ThreadingHTTPServer(("0.0.0.0", STREAM_PORT), make_handler(src))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"MJPEG stream on http://0.0.0.0:{STREAM_PORT}/stream")
    return server


# ------------------------------------------------------------ inspection

def inspect(src):
    """Vote over several FRESH frames so one bad grab cannot decide a disk."""
    votes = []
    _, n = src.latest()
    for _ in range(SAMPLES):
        frame, n = src.next_frame(n)
        if frame is None:
            continue
        res, p, r = classify(frame)
        votes.append(res)
        src.last_result = (res, p, r)
        print(f"    frame: {res}  pass={p} reject={r}")
    if not votes:
        return 3                                 # no frames = camera problem
    return max(set(votes), key=votes.count)



# ------------------------------------------------------------- interface

class Vision:
    """Real camera: owns the camera thread and the MJPEG stream."""

    def __init__(self):
        if cv2 is None:
            raise RuntimeError("OpenCV is not installed (pip install opencv-python)")
        self.cap = open_camera()
        print(f"camera {CAM_INDEX} open, ROI {ROI}")
        self.src = FrameSource(self.cap)
        self.server = start_stream(self.src)

    def inspect(self):
        return inspect(self.src)

    def new_wafer(self):
        self.src.last_result = None

    def close(self):
        self.server.shutdown()
        self.src.stop()
        self.cap.release()


class SimVision:
    """Stand-in for plc_sim.py: reads the simulator-only register 400100."""
    SIM_TAPE_ADDR = 99          # protocol address of 400100

    def __init__(self, host, port):
        from pymodbus.client import ModbusTcpClient
        self.plc = ModbusTcpClient(host, port=port)
        if not self.plc.connect():
            raise RuntimeError(f"sim vision: cannot reach the simulator at {host}:{port}")

    def inspect(self):
        time.sleep(0.5)                          # roughly 5 frames
        rr = self.plc.read_holding_registers(self.SIM_TAPE_ADDR, count=1)
        if rr.isError():
            raise RuntimeError("sim vision: register 400100 not available "
                               "- is this the simulator?")
        result = {1: 1, 2: 2}.get(rr.registers[0], 3)
        print(f"  [sim] camera sees result {result}")
        return result

    def new_wafer(self):
        pass

    def close(self):
        self.plc.close()


def make_vision(sim=False, host="127.0.0.1", port=5020):
    return SimVision(host, port) if sim else Vision()


if __name__ == "__main__":
    if "--cams" in sys.argv:
        list_cams()
    elif "--setup" in sys.argv:
        setup()
    else:
        print(__doc__)
