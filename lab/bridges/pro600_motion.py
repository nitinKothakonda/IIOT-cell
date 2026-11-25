"""pro600_motion.py - PROVIDED. Do not modify.

myCobot Pro 600 motion for the wafer-handling cell: the taught joint poses
and the pick-from-carrier / place-in-rack sequence, already tested on the
real cell.

    from pro600_motion import connect_pro600
    arm = connect_pro600(sim=False)            # real robot at 192.168.10.20
    arm.run_job(1, vacuum_on, vacuum_release, on_clear)  # blocks until finished

Jobs:
    1  pick from the carrier, place in the BLUE rack  (inspection pass)
    2  go home
    4  pick from the carrier, place in the BLACK rack (inspection reject)

The vacuum is switched by the PLC (coils Vac_On and Vac_Blow), so the arm
cannot do it itself. You pass in functions and run_job() calls them at
the right moments:

    vacuum_on()       called when the cup is on the wafer.
                      The arm then waits 2 s for the vacuum to build.
    on_clear()        called when the wafer has been lifted off the carrier
                      and the arm has swung clear of it (pose CLEAR1). From
                      that moment the conveyor may move the carrier back.
    vacuum_release()  called when the wafer is over the rack slot.
                      Must switch the vacuum off and give a short blow-off.

run_job() raises an exception if the robot connection fails.

With sim=True no robot is used: run_job() waits for about as long as the
real motion (four times faster) and still calls your functions at the
right moments.

While moving, the real arm broadcasts its joint angles on UDP port 5005
(JSON, 20 Hz) for the digital twin.
"""
import json
import socket
import time

ROBOT_IP = "192.168.10.20"
SPEED = 1900
VAC_SETTLE = 2.0                   # s with the cup on the wafer before lifting

BCAST_ADDR = ("192.168.10.255", 5005)
BCAST_HZ = 20
ARRIVE_TOL = 1.0                   # degrees, per joint
ARRIVE_N = 3

# ---- joint poses J1..J6, degrees ----
HOME       = [ -81.287, -102.418, 130.242, -118.125, -89.736,  -2.109]
PICK       = [-108.528,  -57.633, 107.217, -139.395, -89.736, -27.619]
APPROACH   = [-108.528,  -60.868, 106.694, -135.791, -89.791, -29.531]  # straight above PICK
CLEAR1     = [-111.085,  -82.860, 125.223, -131.572, -89.561, -31.992]  # route around
CLEAR2     = [ -77.347,  -76.503, 117.508, -131.133, -89.385,   1.670]  # the gantry
BLUE_OVER  = [ -54.726,  -47.095,  85.058, -128.057, -89.736,  24.697]
BLUE_DROP  = [ -54.726,  -45.580,  85.290, -130.166, -89.736,  25.049]
BLACK_OVER = [ -42.202,  -60.602, 110.394, -140.186, -89.824,  37.354]
BLACK_DROP = [ -42.099,  -58.708, 110.337, -141.943, -90.088,  37.529]

JOB_NAMES = {1: "place in BLUE rack", 2: "go home", 4: "place in BLACK rack"}


class Pro600:
    def __init__(self, ip=ROBOT_IP):
        from pymycobot import ElephantRobot      # only needed on the real cell
        self.arm = ElephantRobot(ip, 5001)
        self.arm.start_client()
        time.sleep(1)
        print("Pro 600 connected, at", self.arm.get_angles())
        self.bcast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.bcast.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.bcast.setblocking(False)

    def _publish(self, angles, label):
        if not angles:
            return
        try:
            self.bcast.sendto(json.dumps({"t": time.time(), "device": "pro600",
                                          "label": label, "angles": list(angles)}
                                         ).encode(), BCAST_ADDR)
        except OSError:
            pass

    def goto(self, pose, label):
        print(f"  -> {label}")
        self.arm.write_angles(pose, SPEED)
        settled, t0 = 0, time.time()
        while time.time() - t0 < 60:            # broadcast while moving
            a = self.arm.get_angles()
            self._publish(a, label)
            if a and len(a) == len(pose) and all(
                    abs(c - t) < ARRIVE_TOL for c, t in zip(a, pose)):
                settled += 1
                if settled >= ARRIVE_N:
                    break
            else:
                settled = 0
            time.sleep(1.0 / BCAST_HZ)
        self.arm.command_wait_done()            # the authority on completion
        time.sleep(0.5)
        self._publish(self.arm.get_angles(), label + " (settled)")

    def _transfer(self, rack, vacuum_on, vacuum_release, on_clear):
        over, drop = (BLUE_OVER, BLUE_DROP) if rack == "blue" else (BLACK_OVER, BLACK_DROP)
        self.goto(APPROACH, "above wafer")
        self.goto(PICK, "down to wafer")
        vacuum_on()
        time.sleep(VAC_SETTLE)
        self.goto(APPROACH, "lift straight up")
        self.goto(CLEAR1, "clearance 1")
        if on_clear:
            on_clear()                  # clear of the carrier: it may return
        self.goto(CLEAR2, "clearance 2")
        self.goto(over, f"traverse to {rack}")
        self.goto(drop, "down to drop")
        vacuum_release()
        self.goto(over, "lift")
        self.goto(HOME, "home")

    def run_job(self, job, vacuum_on, vacuum_release, on_clear=None):
        if job == 1:
            self._transfer("blue", vacuum_on, vacuum_release, on_clear)
        elif job == 4:
            self._transfer("black", vacuum_on, vacuum_release, on_clear)
        elif job == 2:
            self.goto(HOME, "home")
        else:
            raise ValueError(f"unknown Pro 600 job {job}")


class SimPro600:
    """Stand-in for testing with plc_sim.py: no robot, realistic timing."""

    def __init__(self, speedup=4.0):
        self.speedup = speedup

    def _wait(self, t):
        time.sleep(t / self.speedup)

    def run_job(self, job, vacuum_on, vacuum_release, on_clear=None):
        if job not in JOB_NAMES:
            raise ValueError(f"unknown Pro 600 job {job}")
        print(f"  [sim] Pro 600 moving ({JOB_NAMES[job]})")
        if job == 2:
            self._wait(3.0)
            return
        self._wait(4.0)             # to the wafer
        vacuum_on()
        self._wait(VAC_SETTLE)
        self._wait(3.0)             # lift straight up, swing to CLEAR1
        if on_clear:
            on_clear()
        self._wait(9.0)             # clearance 2, traverse, down to drop
        vacuum_release()
        self._wait(4.0)             # lift and home


def connect_pro600(sim=False):
    return SimPro600() if sim else Pro600()
