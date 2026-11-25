#!/usr/bin/env python3
"""SOLUTION - instructor copy. vision_bridge.py - Lab 1: inspection camera bridge.

The vision station has no command register. Instead it watches the
sequencer step. When the PLC reaches step 7 (wafer at the pick point,
waiting for inspection), the bridge classifies the wafer once and writes
the result to the Vision_Result register:

    1  pass   (blue tape)  -> the PLC sends the Pro 600 to the BLUE rack
    2  reject (green tape) -> the PLC sends the Pro 600 to the BLACK rack
    3  no tape found       -> the PLC faults

    python vision_bridge.py --sim     # against plc_sim.py, no camera
    python vision_bridge.py           # against the real PLC and camera

The camera and colour classification are already written (vision_core.py).
Your job is the communication with the PLC: complete every TODO.
"""
import argparse
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from vision_core import make_vision

PLC_IP, PLC_PORT = "192.168.10.10", 502    # real cell
SIM_IP, SIM_PORT = "127.0.0.1", 5020       # plc_sim.py

# Register map - see the Cell Modbus TCP Interface sheet.
STEP_REG = 30      # TODO 1 (solved): Step register          (400031)
VIS_REG = 40       # TODO 1 (solved): Vision_Result register (400041)

STEP_INSPECT = 7   # sequencer step that waits for the inspection
POLL = 0.1
RESULT_NAMES = {1: "PASS -> blue rack", 2: "REJECT -> black rack", 3: "NO TAPE -> fault"}


def read_step(plc):
    """Return the current sequencer step, or None if the read failed."""
    # TODO 2 (solved): read one holding register at STEP_REG.
    #         Return its value, or None if the response is an error.
    rr = plc.read_holding_registers(STEP_REG, count=1)
    if rr.isError():
        return None
    return rr.registers[0]


def write_result(plc, result):
    """Write the inspection result. Raise RuntimeError if it fails."""
    # TODO 3 (solved): write `result` to the holding register at VIS_REG.
    #         Raise RuntimeError if the response is an error.
    rr = plc.write_register(VIS_REG, result)
    if rr.isError():
        raise RuntimeError(f"could not write result {result}: {rr}")


def main():
    ap = argparse.ArgumentParser(description="Vision bridge")
    ap.add_argument("--sim", action="store_true", help="use plc_sim.py, no camera")
    ap.add_argument("--host", help="PLC address (overrides the default)")
    ap.add_argument("--port", type=int, help="PLC port (overrides the default)")
    args = ap.parse_args()
    host = args.host or (SIM_IP if args.sim else PLC_IP)
    port = args.port or (SIM_PORT if args.sim else PLC_PORT)

    # TODO 4 (solved): create a Modbus TCP client for host:port and connect it.
    #         If the connection fails, print a message and return.
    plc = ModbusTcpClient(host, port=port)
    if not plc.connect():
        print(f"could not reach the PLC at {host}:{port}")
        return
    print(f"PLC connected at {host}:{port}")

    vision = make_vision(sim=args.sim, host=host, port=port)
    print(f"watching for step {STEP_INSPECT}...\n")

    inspected = False      # True once this wafer has been inspected
    try:
        while True:
            try:
                step = read_step(plc)
            except ModbusException as e:       # connection lost
                print("  PLC connection problem:", e)
                time.sleep(1.0)
                plc.connect()
                continue
            if step is None:
                time.sleep(0.5)
                continue

            # TODO 5 (solved): inspect each wafer exactly once.
            # - when the step is STEP_INSPECT and this wafer has not been
            #   inspected yet: result = vision.inspect(), print it using
            #   RESULT_NAMES, write it to the PLC, and remember that this
            #   wafer is done
            # - when the step is anything else, get ready for the next
            #   wafer (and call vision.new_wafer() to clear the stream overlay)
            if step == STEP_INSPECT and not inspected:
                print(f"step {STEP_INSPECT} - inspecting")
                result = vision.inspect()
                print(f"  result {result}: {RESULT_NAMES[result]}")
                write_result(plc, result)
                inspected = True
            elif step != STEP_INSPECT and inspected:
                inspected = False
                vision.new_wafer()

            time.sleep(POLL)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        vision.close()
        plc.close()


if __name__ == "__main__":
    main()
