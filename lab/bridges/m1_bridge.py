#!/usr/bin/env python3
"""SOLUTION - instructor copy. m1_bridge.py - Lab 1: Dobot M1 Pro bridge.

Connects the M1 Pro to the PLC. The PLC is the Modbus TCP server; this
script is a Modbus TCP client. It watches the M1 Pro command register,
runs the job the PLC asks for, and reports progress in the M1 Pro status
register using the command/status handshake.

Jobs: 1 / 3 pick a wafer from level 1 / 2 of the yellow rack and hold it
above the rack; 5 place the held wafer in the carrier.

    python m1_bridge.py --sim     # against plc_sim.py on this computer
    python m1_bridge.py           # against the real PLC and robot

The robot motion is already written (m1_motion.py). Your job is the
communication with the PLC: complete every TODO in this file.
"""
import argparse
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from m1_motion import connect_m1, JOB_NAMES

PLC_IP, PLC_PORT = "192.168.10.10", 502    # real cell
SIM_IP, SIM_PORT = "127.0.0.1", 5020       # plc_sim.py

# Register map - see the Cell Modbus TCP Interface sheet.
# pymodbus wants the PROTOCOL address, not the 4xxxxx number.
CMD_REG = 10       # TODO 1 (solved): M1 Pro command register (400011)
STAT_REG = 11      # TODO 1 (solved): M1 Pro status register  (400012)

IDLE, BUSY, DONE, FAULT, CLEAR = 0, 1, 2, 99, 3
POLL = 0.1         # s between reads of the command register


def read_command(plc):
    """Return the value of the command register, or None if the read failed."""
    # TODO 2 (solved): read one holding register at CMD_REG.
    #         Return its value, or None if the response is an error.
    rr = plc.read_holding_registers(CMD_REG, count=1)
    if rr.isError():
        return None
    return rr.registers[0]


def write_status(plc, value):
    """Write `value` to the status register. Raise RuntimeError if it fails."""
    # TODO 3 (solved): write `value` to the holding register at STAT_REG.
    #         Raise RuntimeError if the response is an error.
    rr = plc.write_register(STAT_REG, value)
    if rr.isError():
        raise RuntimeError(f"could not write status {value}: {rr}")


def wait_for_command_clear(plc):
    """Block until the PLC has written 0 to the command register."""
    # TODO 4 (solved): poll the command register every POLL seconds
    #         and return once it reads 0.
    while read_command(plc) != 0:
        time.sleep(POLL)


def handle_job(plc, m1, cmd):
    """Run one job with the full command/status handshake."""
    print(f"command {cmd}: {JOB_NAMES[cmd]}")

    def on_clear():
        # Called by the motion during a place (job 5), once the fork has
        # withdrawn above the carrier. The arm is still moving home.
        # TODO 5 (solved): tell the PLC the arm is clear of the carrier, so the
        # conveyor can leave without waiting for the job to finish.
        write_status(plc, CLEAR)
        print("    clear of the carrier")

    t0 = time.time()
    # TODO 6 (solved): the handshake.
    # a) tell the PLC you are busy
    # b) run the motion:  m1.run_job(cmd, on_clear)
    # c) tell the PLC you are done
    # d) if the motion raises an exception, report FAULT instead of done
    # e) wait until the PLC clears the command, then go back to idle
    write_status(plc, BUSY)
    try:
        m1.run_job(cmd, on_clear)
        write_status(plc, DONE)
        print(f"  done in {time.time() - t0:.1f} s, waiting for the PLC to clear")
    except Exception as e:                     # robot error or lost connection
        write_status(plc, FAULT)
        print("  FAULT:", e)
    wait_for_command_clear(plc)
    write_status(plc, IDLE)
    print("  idle\n")


def main():
    ap = argparse.ArgumentParser(description="M1 Pro bridge")
    ap.add_argument("--sim", action="store_true", help="use plc_sim.py, no robot")
    ap.add_argument("--host", help="PLC address (overrides the default)")
    ap.add_argument("--port", type=int, help="PLC port (overrides the default)")
    args = ap.parse_args()
    host = args.host or (SIM_IP if args.sim else PLC_IP)
    port = args.port or (SIM_PORT if args.sim else PLC_PORT)

    m1 = connect_m1(sim=args.sim)

    # TODO 7 (solved): create a Modbus TCP client for host:port and connect it.
    #         If the connection fails, print a message and return.
    plc = ModbusTcpClient(host, port=port)
    if not plc.connect():
        print(f"could not reach the PLC at {host}:{port}")
        return
    print(f"PLC connected at {host}:{port}")

    # TODO 8 (solved): start in a known state - write IDLE to the status register.
    write_status(plc, IDLE)
    print("waiting for commands...\n")

    last_unknown = None
    try:
        while True:
            try:
                cmd = read_command(plc)
            except ModbusException as e:       # connection lost
                print("  PLC connection problem:", e)
                time.sleep(1.0)
                plc.connect()
                continue
            if cmd is None:
                time.sleep(0.5)
                continue
            if cmd in JOB_NAMES:
                handle_job(plc, m1, cmd)
            elif cmd != 0 and cmd != last_unknown:
                print(f"unknown command {cmd}, ignoring")
                last_unknown = cmd
            time.sleep(POLL)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        plc.close()


if __name__ == "__main__":
    main()
