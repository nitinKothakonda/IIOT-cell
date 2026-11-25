"""m1_motion.py - PROVIDED. Do not modify.

Dobot M1 Pro motion for the wafer-handling cell: the taught points and the
pick-and-place sequences, already tested on the real cell.

    from m1_motion import connect_m1
    m1 = connect_m1(sim=False)     # real robot at 192.168.10.40
    m1.run_job(1)                  # blocks until the motion is finished
    m1.run_job(5, on_clear)        # place: calls on_clear() part way through

Jobs:
    1  pick from yellow level 1 (top) and hold the wafer above the rack
    3  pick from yellow level 2 (lower) and hold the wafer above the rack
    5  place the held wafer in the carrier, then go home
    2  go home

A pick ends with the wafer on the fork, above the rack, out of the
conveyor's way. The robot waits there until the PLC sends job 5.

During job 5, run_job() calls on_clear() as soon as the fork has
withdrawn above the carrier (pose P7), while the arm is still moving home.
From that moment the conveyor may move the carrier.

run_job() raises an exception if the robot reports an error or the
connection drops. It knows nothing about the PLC - that is your job.

With sim=True no robot is used: run_job() waits for about as long as
the real motion takes (four times faster), and still calls on_clear()
at the right moment, so you can test against plc_sim.py.
"""
import socket
import time

M1_IP = "192.168.10.40"
SPEED = 20          # SpeedFactor, % of maximum
SETTLE = 0.3        # s after each move

# ---- shared / disk 1, top level  (X, Y, Z mm, R deg) ----
HOME = (314.38,  41.65, 157.12,  95.88)
P1   = (314.38,  41.65,  70.53,  95.88)
P2   = (314.13, 180.89,  70.53,  95.88)
P3   = (314.13, 180.89, 111.93,  95.88)
P4   = ( 69.49, 389.61, 110.40, 184.58)
P5   = ( 69.49, 389.61,  75.62, 184.58)
P6   = (203.78, 340.41,  75.62, 179.87)
P7   = (203.78, 340.41, 151.12, 179.87)

# ---- disk 2, lower level ----
Z_DISK2      =  32.68
Z_SMALL_LIFT =  49.28
Y_RETRACT    =  41.65
Z_LIFT_CLEAR = 111.93

Q1 = (314.38,  41.65,    Z_DISK2,      95.88)
Q2 = (314.13, 180.89,    Z_DISK2,      95.88)
Q3 = (314.13, 180.89,    Z_SMALL_LIFT, 95.88)
Q4 = (314.38, Y_RETRACT, Z_SMALL_LIFT, 95.88)
Q5 = (314.38, Y_RETRACT, Z_LIFT_CLEAR, 95.88)

JOB_NAMES = {1: "pick yellow level 1", 3: "pick yellow level 2",
             5: "place in carrier", 2: "go home"}


class M1:
    """Real robot over the Dobot TCP API (dashboard 29999, motion 30003)."""

    def __init__(self, ip=M1_IP):
        self.dash = socket.create_connection((ip, 29999), timeout=10)
        self.move = socket.create_connection((ip, 30003), timeout=60)

    def _send(self, sock, cmd):
        sock.sendall((cmd + "\n").encode())
        reply = sock.recv(1024).decode().strip()
        if reply and not reply.startswith("0,"):
            print(f"    warn: {cmd} -> {reply}")
        return reply

    def enable(self):
        self._send(self.dash, "ClearError()")
        self._send(self.dash, "EnableRobot()")
        time.sleep(2)
        self._send(self.dash, f"SpeedFactor({SPEED})")

    def goto(self, pose, label, kind="J"):
        # MovL only inside the rack (fork in a slot); MovJ in free space
        x, y, z, r = pose
        print(f"  -> {label}")
        self._send(self.move, f"{'MovL' if kind == 'L' else 'MovJ'}({x},{y},{z},{r})")
        self._send(self.move, "Sync()")
        time.sleep(SETTLE)

    # ---- jobs ----
    def _pick_l1(self):
        # ends holding the wafer at P3, above the rack
        self.goto(P1, "descend to disk 1", "J")
        self.goto(P2, "traverse low",      "L")
        self.goto(P3, "lift clear (hold)", "J")

    def _pick_l2(self):
        # level 1's shelf is above: unseat, retract in -Y, then lift.
        # Ends holding the wafer at Q5, above the rack.
        self.goto(Q1, "descend to disk 2",  "J")
        self.goto(Q2, "traverse low",       "L")
        self.goto(Q3, "small lift",         "L")
        self.goto(Q4, "retract from rack",  "L")
        self.goto(Q5, "lift clear (hold)",  "J")

    def _place(self, on_clear):
        # from the hold pose into the carrier; all free space, so all MovJ
        self.goto(P4,   "over conveyor",      "J")
        self.goto(P5,   "descend to carrier", "J")
        self.goto(P6,   "rotate",             "J")
        self.goto(P7,   "withdraw",           "J")
        if on_clear:
            on_clear()                  # fork is above the carrier: conveyor may go
        self.goto(HOME, "home",               "J")

    def run_job(self, job, on_clear=None):
        if job == 1:
            self._pick_l1()
        elif job == 3:
            self._pick_l2()
        elif job == 5:
            self._place(on_clear)
        elif job == 2:
            self.goto(HOME, "home", "J")
        else:
            raise ValueError(f"unknown M1 job {job}")


class SimM1:
    """Stand-in for testing with plc_sim.py: no robot, realistic timing.
    Times (s) on the real cell: (until clear, total). None = no clear."""
    TIMES = {1: (None, 9.0), 3: (None, 18.0), 5: (9.0, 12.0), 2: (None, 3.0)}

    def __init__(self, speedup=4.0):
        self.speedup = speedup

    def run_job(self, job, on_clear=None):
        if job not in self.TIMES:
            raise ValueError(f"unknown M1 job {job}")
        t_clear, t_done = (t / self.speedup if t else None for t in self.TIMES[job])
        print(f"  [sim] M1 Pro moving ({JOB_NAMES[job]}, {t_done:.1f} s)")
        if t_clear is not None:
            time.sleep(t_clear)
            if on_clear:
                on_clear()
            time.sleep(t_done - t_clear)
        else:
            time.sleep(t_done)


def connect_m1(sim=False):
    if sim:
        return SimM1()
    m1 = M1()
    m1.enable()
    print("M1 Pro enabled")
    return m1
