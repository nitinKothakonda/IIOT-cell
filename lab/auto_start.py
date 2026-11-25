"""Start the cell again whenever a wafer is placed in the yellow rack.

The PLC program and the three bridges are not changed. This script only
watches the PLC and writes the Start_Cmd coil (the same thing
`python explore.py --start` does), once per wafer.

It starts a cycle only when ALL of these hold for --settle seconds:
  - the cell is idle (step 0, Start_Cmd off)
  - there is no fault and the e-stop circuit is OK
  - a wafer is detected on yellow level 1 or 2

After a start it waits for the cycle to finish. If a fault appears it
stops and does NOT reset anything: a person decides what to do.

    python lab\\auto_start.py                 # loop until Ctrl+C
    python lab\\auto_start.py --max 6         # stop after 6 cycles

Keep a second person at the e-stop whenever this is running.
"""
import argparse
import time

from pymodbus.client import ModbusTcpClient

START, FAULT, ESTOP_OK, YELLOW_L1, YELLOW_L2 = 2, 4, 5, 6, 7
STEP_REG = 30


def read_state(plc):
    c = plc.read_coils(0, count=17)
    h = plc.read_holding_registers(STEP_REG, count=1)
    if c.isError() or h.isError():
        return None
    b = c.bits
    return {"start": b[START], "fault": b[FAULT], "estop_ok": b[ESTOP_OK],
            "wafer": b[YELLOW_L1] or b[YELLOW_L2], "step": h.registers[0]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.10.10")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--settle", type=float, default=3.0,
                    help="s a wafer must stay detected before a start")
    ap.add_argument("--max", type=int, default=0, help="stop after N cycles (0 = no limit)")
    a = ap.parse_args()

    plc = ModbusTcpClient(a.host, port=a.port)
    if not plc.connect():
        raise SystemExit(f"cannot reach the PLC at {a.host}:{a.port}")
    print(f"connected to {a.host}:{a.port}; waiting for a wafer in the yellow rack (Ctrl+C to stop)")

    cycles, seen_since = 0, None
    try:
        while a.max == 0 or cycles < a.max:
            s = read_state(plc)
            if s is None:
                print("read failed, retrying"); time.sleep(1); continue
            if s["fault"]:
                print("PLC fault: stopping. Not resetting anything; check the cell first.")
                break
            idle = s["step"] == 0 and not s["start"] and s["estop_ok"]
            if idle and s["wafer"]:
                seen_since = seen_since or time.time()
                if time.time() - seen_since >= a.settle:
                    plc.write_coil(START, True)
                    cycles += 1
                    seen_since = None
                    print(f"cycle {cycles}: START sent")
                    t0 = time.time()                     # wait for it to begin ...
                    while time.time() - t0 < 20:
                        s = read_state(plc)
                        if s and (s["step"] != 0 or s["fault"]):
                            break
                        time.sleep(0.2)
                    while True:                          # ... and to finish
                        s = read_state(plc)
                        if s and s["fault"]:
                            print("PLC fault during the cycle: stopping."); raise SystemExit(1)
                        if s and s["step"] == 0 and not s["start"]:
                            print(f"cycle {cycles}: done"); break
                        time.sleep(0.5)
            else:
                seen_since = None
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        plc.close()
    print(f"cycles started: {cycles}")


if __name__ == "__main__":
    main()
