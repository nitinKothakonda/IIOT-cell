#!/usr/bin/env python3
"""SOLUTION - instructor copy. pro600_bridge.py - Lab 1: myCobot Pro 600 bridge.

Connects the Pro 600 to the PLC. It watches the Pro 600 command register,
picks the wafer from the carrier and places it in the rack the PLC asks
for, and reports progress in the Pro 600 status register.

The vacuum cup is switched by the PLC's outputs, so this bridge also
writes two coils: Vac_On (hold the wafer) and Vac_Blow (blow-off pulse
to release it).

    python pro600_bridge.py --sim     # against plc_sim.py on this computer
    python pro600_bridge.py           # against the real PLC and robot

The robot motion is already written (pro600_motion.py). Your job is the
communication with the PLC: complete every TODO in this file.
"""
import argparse
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from pro600_motion import connect_pro600, JOB_NAMES

PLC_IP, PLC_PORT = "192.168.10.10", 502    # real cell
SIM_IP, SIM_PORT = "127.0.0.1", 5020       # plc_sim.py

# Register map - see the Cell Modbus TCP Interface sheet.
# pymodbus wants the PROTOCOL address, not the 4xxxxx / 0xxxxx number.
CMD_REG = 0        # TODO 1 (solved): Pro 600 command register (400001)
STAT_REG = 1       # TODO 1 (solved): Pro 600 status register  (400002)
VAC_ON = 0         # TODO 1 (solved): Vac_On coil   (000001)
VAC_BLOW = 1       # TODO 1 (solved): Vac_Blow coil (000002)

IDLE, BUSY, DONE, FAULT, CLEAR = 0, 1, 2, 99, 3
POLL = 0.1         # s between reads of the command register
BLOW_TIME = 1.5    # s of blow-off to release the wafer


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


def set_coil(plc, coil, on):
    """Switch one coil on or off. Raise RuntimeError if it fails."""
    # TODO 4 (solved): write `on` (True/False) to the coil at address `coil`.
    #         Raise RuntimeError if the response is an error.
    rr = plc.write_coil(coil, on)
    if rr.isError():
        raise RuntimeError(f"could not write coil {coil}: {rr}")


def wait_for_command_clear(plc):
    """Block until the PLC has written 0 to the command register."""
    # TODO 5 (solved): poll the command register every POLL seconds
    #         and return once it reads 0.
    while read_command(plc) != 0:
        time.sleep(POLL)


def handle_job(plc, arm, cmd):
    """Run one job with the full command/status handshake."""
    print(f"command {cmd}: {JOB_NAMES[cmd]}")

    def vacuum_on():
        # TODO 6 (solved): switch the vacuum on (Vac_On coil).
        set_coil(plc, VAC_ON, True)
        print("    vacuum on")

    def vacuum_release():
        # TODO 7 (solved): release the wafer: vacuum off, then a blow-off pulse
        #         of BLOW_TIME seconds (Vac_Blow on, wait, Vac_Blow off).
        set_coil(plc, VAC_ON, False)
        set_coil(plc, VAC_BLOW, True)
        time.sleep(BLOW_TIME)
        set_coil(plc, VAC_BLOW, False)
        print("    released")

    def on_clear():
        # Called by the motion once the wafer is lifted off the carrier and
        # the arm has swung clear of it. The arm is still moving to the rack.
        # TODO 8 (solved): tell the PLC the arm is clear of the carrier, so the
        # carrier can return without waiting for the job to finish.
        write_status(plc, CLEAR)
        print("    clear of the carrier")

    t0 = time.time()
    # TODO 9 (solved): the handshake.
    # a) tell the PLC you are busy
    # b) run the motion:  arm.run_job(cmd, vacuum_on, vacuum_release, on_clear)
    # c) tell the PLC you are done
    # d) if the motion raises an exception: make sure both vacuum coils
    #    are off, and report FAULT instead of done
    # e) wait until the PLC clears the command, then go back to idle
    write_status(plc, BUSY)
    try:
        arm.run_job(cmd, vacuum_on, vacuum_release, on_clear)
        write_status(plc, DONE)
        print(f"  done in {time.time() - t0:.1f} s, waiting for the PLC to clear")
    except Exception as e:
        set_coil(plc, VAC_ON, False)
        set_coil(plc, VAC_BLOW, False)
        write_status(plc, FAULT)
        print("  FAULT:", e)
    wait_for_command_clear(plc)
    write_status(plc, IDLE)
    print("  idle\n")


def main():
    ap = argparse.ArgumentParser(description="Pro 600 bridge")
    ap.add_argument("--sim", action="store_true", help="use plc_sim.py, no robot")
    ap.add_argument("--host", help="PLC address (overrides the default)")
    ap.add_argument("--port", type=int, help="PLC port (overrides the default)")
    args = ap.parse_args()
    host = args.host or (SIM_IP if args.sim else PLC_IP)
    port = args.port or (SIM_PORT if args.sim else PLC_PORT)

    arm = connect_pro600(sim=args.sim)

    # TODO 10 (solved): create a Modbus TCP client for host:port and connect it.
    #         If the connection fails, print a message and return.
    plc = ModbusTcpClient(host, port=port)
    if not plc.connect():
        print(f"could not reach the PLC at {host}:{port}")
        return
    print(f"PLC connected at {host}:{port}")

    # TODO 11 (solved): start in a known state - status IDLE, both vacuum coils off.
    write_status(plc, IDLE)
    set_coil(plc, VAC_ON, False)
    set_coil(plc, VAC_BLOW, False)
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
                handle_job(plc, arm, cmd)
            elif cmd != 0 and cmd != last_unknown:
                print(f"unknown command {cmd}, ignoring")
                last_unknown = cmd
            time.sleep(POLL)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        # never leave the vacuum on when the bridge stops
        try:
            set_coil(plc, VAC_ON, False)
            set_coil(plc, VAC_BLOW, False)
        except Exception:
            pass
        plc.close()


if __name__ == "__main__":
    main()
