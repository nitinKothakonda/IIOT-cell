#!/usr/bin/env python3
"""SOLUTION - instructor copy. explore.py - Lab 1, Part A: talk to the PLC with a Modbus TCP client.

Reads the cell's state once and prints it, then (with --start) presses
the START coil and watches the step register for 20 seconds.

    python explore.py --sim            # against plc_sim.py
    python explore.py --sim --start
    python explore.py                  # against the real PLC (read only)

Complete every TODO.
"""
import argparse
import time

from pymodbus.client import ModbusTcpClient

PLC_IP, PLC_PORT = "192.168.10.10", 502
SIM_IP, SIM_PORT = "127.0.0.1", 5020

COIL_NAMES = ["Vac_On", "Vac_Blow", "Start_Cmd", "Fault_Reset", "Fault", "EStop_OK",
              "Yellow_L1", "Yellow_L2", "Blue_L1", "Blue_L2", "Black_L1", "Black_L2",
              "Carrier_Datum", "Disk_In_Carrier", "Vac_Present", "Homed"]


def main():
    ap = argparse.ArgumentParser(description="Explore the PLC over Modbus TCP")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--start", action="store_true", help="press START and watch the step")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    host = args.host or (SIM_IP if args.sim else PLC_IP)
    port = args.port or (SIM_PORT if args.sim else PLC_PORT)

    plc = ModbusTcpClient(host, port=port)
    if not plc.connect():
        print(f"could not reach the PLC at {host}:{port}")
        return

    # TODO A1 (solved): read the 16 coils 000001-000016 in ONE request and print
    #          each name from COIL_NAMES with its value.
    rr = plc.read_coils(0, count=16)
    if rr.isError():
        print("coil read failed:", rr)
    else:
        for name, value in zip(COIL_NAMES, rr.bits):
            print(f"  {name:16s} {int(value)}")

    # TODO A2 (solved): read and print the carrier step, the M1 Pro state and the
    #          Pro 600 state (400031-400033), the fault code (400051), the
    #          pass and reject counts (400052-400053) and the heartbeat
    #          (400061). Use as few requests as you can - but note that a
    #          request covering an address that is not in the map fails.
    states = plc.read_holding_registers(30, count=3)
    faults = plc.read_holding_registers(50, count=3)
    hb = plc.read_holding_registers(60, count=1)
    if states.isError() or faults.isError() or hb.isError():
        print("register read failed")
    else:
        step, m1_state, pro_state = states.registers
        print(f"  step {step}   M1 Pro state {m1_state}   Pro 600 state {pro_state}")
        print(f"  fault {faults.registers[0]}   pass {faults.registers[1]}   "
              f"reject {faults.registers[2]}   heartbeat {hb.registers[0]}")

    if args.start:
        # TODO A3 (solved): write True to the Start_Cmd coil (000003), then for 20
        #          seconds print the step and the two robot states every
        #          time any of them changes.
        plc.write_coil(2, True)
        last, t0 = None, time.time()
        while time.time() - t0 < 20:
            rr = plc.read_holding_registers(30, count=3)
            if not rr.isError() and rr.registers != last:
                last = rr.registers
                print(f"  {time.time() - t0:5.1f} s  step {last[0]:>2}   "
                      f"M1 Pro {last[1]}   Pro 600 {last[2]}")
            time.sleep(0.1)

    plc.close()


if __name__ == "__main__":
    main()
