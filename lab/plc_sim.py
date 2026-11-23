#!/usr/bin/env python3
"""
plc_sim.py - Modbus TCP simulator of the wafer-handling cell PLC (Micro850).

Runs the same sequencer as the real PLC and
serves the same register map, so your bridges can be developed and tested
without the physical cell. No robots move: the simulator only plays the PLC,
the conveyor and the rack sensors.

    python plc_sim.py                    # listens on 127.0.0.1:5020
    python plc_sim.py --wafers blue,green --port 5020

Point your bridges at host 127.0.0.1, port 5020 (the real PLC is
192.168.10.10, port 502).

Differences from the real cell:
  - Conveyor, homing and racks are simulated with realistic timing.
  - Robots are not simulated. When a bridge reports progress, the simulator
    moves the wafer as the robot would have: an M1 Pro pick (job 1 or 3)
    leaves it on the fork at done; an M1 Pro place (job 5) puts it in the
    carrier at clear (status 3); a Pro 600 sort takes it off the carrier at
    clear and into the rack at done.
  - Holding register 400100 (SIM ONLY) holds the tape colour of the wafer
    in the carrier: 0 none, 1 blue, 2 green. Use it to test the vision
    bridge's Modbus logic without a camera. It does not exist on the real PLC.
  - Reading or writing an address that is not in the map returns Modbus
    exception 02 (Illegal Data Address).

Type `help` in the simulator window for commands (reload wafers, E-stop,
jam the conveyor, ...).

No third-party packages needed. Python 3.8+.
"""
import argparse
import asyncio
import struct
import sys
import threading
import time

SCAN = 0.01  # PLC scan period, s

# --------------------------------------------------------------------------
# Register map (protocol addresses, 0-based) -> variable name
# --------------------------------------------------------------------------
HOLDING = {
    0: "Cmd_Word",        # 400001 Pro 600 command
    1: "Status_Word",     # 400002 Pro 600 status
    10: "Cmd_Word_2",     # 400011 M1 Pro command
    11: "Status_Word_2",  # 400012 M1 Pro status
    30: "Step_Mirror",    # 400031 carrier step
    31: "M1_State_Mirror",   # 400032
    32: "Pro_State_Mirror",  # 400033
    40: "Vision_Result",  # 400041
    50: "Fault_Code",     # 400051
    51: "Pass_Count",     # 400052
    52: "Reject_Count",   # 400053
    60: "PLC_Heartbeat",  # 400061
    99: "Sim_Tape",       # 400100 SIM ONLY
}
COILS = {
    0: "Vac_On", 1: "Vac_Blow", 2: "Start_Cmd", 3: "Fault_Reset", 4: "Fault",
    5: "EStop_OK", 6: "Yellow_L1", 7: "Yellow_L2", 8: "Blue_L1", 9: "Blue_L2",
    10: "Black_L1", 11: "Black_L2", 12: "Carrier_Datum", 13: "Disk_In_Carrier",
    14: "Vac_Present", 15: "Homed", 16: "Home_Req",
}

STEP_NAMES = {
    0: "idle", 90: "homing conveyor", 1: "carrier at load point, waiting for M1 Pro",
    2: "M1 Pro clear: checking wafer in carrier", 6: "conveyor -> pick point",
    7: "vision: waiting for result", 8: "at pick point: Pro 600 -> BLUE (pass)",
    9: "at pick point: Pro 600 -> BLACK (reject)",
    11: "conveyor -> load point", 12: "check carrier home",
}
M1_NAMES = {0: "idle", 1: "ready check (pick)", 2: "picking -> hold pose",
            3: "acknowledge pick", 4: "holding wafer above the rack",
            5: "ready check (place)", 6: "placing in carrier", 7: "acknowledge place"}
PRO_NAMES = {0: "idle", 1: "ready check", 2: "sorting", 3: "acknowledge"}
FAULTS = {
    1: "a device reported status 99",
    2: "no wafer: yellow rack empty when one is needed",
    3: "wafer not detected in carrier",
    4: "vision found no tape",
    5: "carrier did not return to the load point",
    6: "M1 Pro did not report done (2) in time",
    7: "M1 Pro status did not return to 0 after the job",
    8: "Pro 600 did not report done (2) in time",
    9: "Pro 600 status did not return to 0 after the job",
    10: "no vision result in time",
    11: "conveyor move or homing timed out",
    12: "device not ready: status was not 0 when a job was due",
    13: "E-stop opened during a cycle",
    14: "a device went busy (1 or 3) -> idle (0) without reporting done (2)",
    15: "carrier not empty when the M1 Pro was about to place a wafer",
}

TAPE_CODE = {None: 0, "blue": 1, "green": 2, "none": 0}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------
# PLC: line-for-line port of sequencer_ameri.st
# --------------------------------------------------------------------------
class TON:
    def __init__(self):
        self.et = 0.0
        self.q = False

    def __call__(self, IN, PT):
        self.et = min(self.et + SCAN, PT) if IN else 0.0
        self.q = IN and self.et >= PT - 1e-9


class Sequencer:
    def __init__(self):
        for name in list(HOLDING.values()) + list(COILS.values()):
            setattr(self, name, 0 if name in HOLDING.values() else False)
        self.EStop_OK = True
        self.Step = self.Last_Step = 0
        self.M1_State = self.M1_Last = 0
        self.Pro_State = self.Pro_Last = 0
        self.M1_Job = self.Pro_Job = 0
        self.M1_Has_Wafer = self.M1_Placed = self.M1_Busy_Seen = False
        self.Pro_Clear = self.Pro_Clear_Sent = self.Pro_Busy_Seen = False
        self.Datum_Seen = False
        self.Pos_Cmd = 0.0
        self.Move_Abs_Cmd = self.Abs_Done = self.Home_Cmd = False
        self.Step_Limit = self.M1_Limit = self.Pro_Limit = 0.0
        self.Timeout_Code = self.M1_TCode = self.Pro_TCode = 0
        self.T_HB, self.T_Step, self.T_M1, self.T_Pro = TON(), TON(), TON(), TON()

    def _fault(self, code):
        self.Fault_Code, self.Fault = code, True

    def scan(self):
        self.T_HB(not self.T_HB.q, 1.0)
        if self.T_HB.q:
            self.PLC_Heartbeat = 0 if self.PLC_Heartbeat >= 60000 else self.PLC_Heartbeat + 1
        self.Step_Limit, self.Timeout_Code = 0.0, 0
        self.M1_Limit, self.M1_TCode = 0.0, 0
        self.Pro_Limit, self.Pro_TCode = 0.0, 0
        if not self.Fault:
            self.Fault_Reset = False

        if self.Fault:
            self.Cmd_Word = self.Cmd_Word_2 = 0
            self.Move_Abs_Cmd = self.Home_Cmd = False
            if self.Fault_Reset:
                if self.M1_State in (2, 3):
                    self.M1_Has_Wafer = True
                elif self.M1_State >= 6:
                    self.M1_Has_Wafer = False
                self.M1_State = 4 if self.M1_Has_Wafer else 0
                self.Pro_State = 0
                self.Fault = self.Fault_Reset = False
                self.Fault_Code = 0
                self.Start_Cmd = False
                self.Homed = False
                self.M1_Job = 0
                self.M1_Placed = self.Pro_Clear = self.Datum_Seen = False
                self.Step = 0
        elif self.Status_Word == 99 or self.Status_Word_2 == 99:
            self._fault(1)
        elif self.Step != 0 and not self.EStop_OK:
            self._fault(13)
        else:
            self._carrier()
            self._m1()
            self._pro()

        stp, m1s, pro = self.Step, self.M1_State, self.Pro_State
        self.T_Step(stp == self.Last_Step and not self.Fault, 600.0)
        self.T_M1(m1s == self.M1_Last and not self.Fault, 600.0)
        self.T_Pro(pro == self.Pro_Last and not self.Fault, 600.0)
        self.Last_Step, self.M1_Last, self.Pro_Last = stp, m1s, pro
        if not self.Fault:
            eps = 1e-9
            if self.Step_Limit > 0 and self.T_Step.et >= self.Step_Limit - eps:
                self._fault(self.Timeout_Code)
            elif self.M1_Limit > 0 and self.T_M1.et >= self.M1_Limit - eps:
                self._fault(self.M1_TCode)
            elif self.Pro_Limit > 0 and self.T_Pro.et >= self.Pro_Limit - eps:
                self._fault(self.Pro_TCode)
        self.Step_Mirror = self.Step
        self.M1_State_Mirror = self.M1_State
        self.Pro_State_Mirror = self.Pro_State

    # ------------------------------------------------------------ carrier
    def _carrier(self):
        s = self.Step
        rack = self.Yellow_L1 or self.Yellow_L2
        if s == 0:
            self.Datum_Seen = False
            if self.Home_Req:
                self.Home_Req = False
                self.Step = 90
            elif self.Start_Cmd and self.EStop_OK:
                if not (rack or self.M1_Has_Wafer):
                    self._fault(2)
                else:
                    self.Step = 1 if self.Homed else 90
        elif s == 1:
            if self.M1_Placed:
                self.M1_Placed = False
                self.Step = 2
            elif self.M1_State == 0 and not self.M1_Has_Wafer and not rack:
                self._fault(2)
        elif s == 2:
            self.Step_Limit, self.Timeout_Code = 5.0, 3
            if self.Disk_In_Carrier:
                self.Step = 6
        elif s == 6:
            self.Step_Limit, self.Timeout_Code = 20.0, 11
            self.Pos_Cmd, self.Move_Abs_Cmd = 170.0, True
            if self.Abs_Done:
                self.Move_Abs_Cmd = False
                self.Vision_Result = 0
                self.Step = 7
        elif s == 7:
            self.Step_Limit, self.Timeout_Code = 15.0, 10
            if self.Vision_Result == 1:
                self.Pass_Count += 1
                self.Pro_Job, self.Step = 1, 8
            elif self.Vision_Result == 2:
                self.Reject_Count += 1
                self.Pro_Job, self.Step = 4, 9
            elif self.Vision_Result == 3:
                self._fault(4)
        elif s in (8, 9):
            if self.Pro_Clear:
                self.Pro_Clear = False
                self.Step = 11
        elif s == 11:
            self.Step_Limit, self.Timeout_Code = 30.0, 11
            self.Pos_Cmd, self.Move_Abs_Cmd = 0.0, True
            if self.Carrier_Datum:
                self.Datum_Seen = True
            if self.Abs_Done:
                self.Move_Abs_Cmd = False
                self.Step = 12
        elif s == 12:
            if self.Datum_Seen or self.Carrier_Datum:
                if self.M1_Has_Wafer or self.M1_State in (1, 2, 3) or rack:
                    self.Datum_Seen = False
                    self.Step = 1
                elif self.Pro_State == 0:
                    self.Datum_Seen = False
                    self.Start_Cmd = False
                    self.Step = 0
            else:
                self._fault(5)
        elif s == 90:
            self.Step_Limit, self.Timeout_Code = 60.0, 11
            self.Home_Cmd = True
            if self.Homed:
                self.Home_Cmd = False
                self.Step = 1 if self.Start_Cmd else 0

    # ------------------------------------------------------------- M1 Pro
    def _m1(self):
        s, sts = self.M1_State, self.Status_Word_2
        if s == 0:
            self.Cmd_Word_2 = 0
            if (self.Start_Cmd and self.Step != 0 and not self.M1_Has_Wafer
                    and (self.Yellow_L1 or self.Yellow_L2)):
                self.M1_State = 1
        elif s == 1:
            self.Cmd_Word_2 = 0
            self.M1_Limit, self.M1_TCode = 10.0, 12
            if sts == 0:
                self.M1_Busy_Seen = False
                if self.Yellow_L1:
                    self.M1_Job, self.M1_State = 1, 2
                elif self.Yellow_L2:
                    self.M1_Job, self.M1_State = 3, 2
                else:
                    self.M1_State = 0
        elif s == 2:
            self.Cmd_Word_2 = self.M1_Job
            self.M1_Limit, self.M1_TCode = 60.0, 6
            if sts == 2:
                self.M1_State = 3
            elif sts in (1, 3):
                self.M1_Busy_Seen = True
            elif self.M1_Busy_Seen and sts == 0:
                self._fault(14)
        elif s == 3:
            self.Cmd_Word_2 = 0
            self.M1_Job = 0
            self.M1_Limit, self.M1_TCode = 10.0, 7
            if sts == 0:
                self.M1_Has_Wafer = True
                self.M1_State = 4
        elif s == 4:
            self.Cmd_Word_2 = 0
            if self.Step == 1:
                if self.Disk_In_Carrier:
                    self._fault(15)
                else:
                    self.M1_State = 5
        elif s == 5:
            self.Cmd_Word_2 = 0
            self.M1_Limit, self.M1_TCode = 10.0, 12
            if sts == 0:
                self.M1_Busy_Seen = False
                self.M1_State = 6
        elif s == 6:
            self.Cmd_Word_2 = 5
            self.M1_Limit, self.M1_TCode = 60.0, 6
            if sts in (3, 2) and self.M1_Has_Wafer:
                self.M1_Has_Wafer = False
                self.M1_Placed = True
            if sts == 2:
                self.M1_State = 7
            elif sts in (1, 3):
                self.M1_Busy_Seen = True
            elif self.M1_Busy_Seen and sts == 0:
                self._fault(14)
        elif s == 7:
            self.Cmd_Word_2 = 0
            self.M1_Limit, self.M1_TCode = 10.0, 7
            if sts == 0:
                self.M1_State = 0

    # ------------------------------------------------------------ Pro 600
    def _pro(self):
        s, sts = self.Pro_State, self.Status_Word
        if s == 0:
            self.Cmd_Word = 0
            if self.Step in (8, 9):
                self.Pro_State = 1
        elif s == 1:
            self.Cmd_Word = 0
            self.Pro_Limit, self.Pro_TCode = 10.0, 12
            if sts == 0:
                self.Pro_Busy_Seen = self.Pro_Clear_Sent = False
                self.Pro_State = 2
        elif s == 2:
            self.Cmd_Word = self.Pro_Job
            self.Pro_Limit, self.Pro_TCode = 60.0, 8
            if sts in (3, 2) and not self.Pro_Clear_Sent:
                self.Pro_Clear_Sent = True
                self.Pro_Clear = True
            if sts == 2:
                self.Pro_State = 3
            elif sts in (1, 3):
                self.Pro_Busy_Seen = True
            elif self.Pro_Busy_Seen and sts == 0:
                self._fault(14)
        elif s == 3:
            self.Cmd_Word = 0
            self.Pro_Limit, self.Pro_TCode = 10.0, 9
            if sts == 0:
                self.Pro_State = 0


# --------------------------------------------------------------------------
# The physical side: conveyor, racks, wafers
# --------------------------------------------------------------------------
class World:
    VEL, ACC = 20.0, 200.0      # axis units/s, units/s^2 (same as the PLC)
    HOME_TIME = 3.0

    def __init__(self, wafers):
        self.pos = 0.0
        self.move_t = 0.0
        self.move_from = 0.0
        self.prev_exec = False
        self.home_t = 0.0
        self.jammed = False
        self.carrier = None            # tape colour of the wafer in the carrier
        self.m1_fork = None            # wafer held by the M1 Pro
        self.pro_cup = None            # wafer held by the Pro 600
        self.yellow = {1: None, 2: None}
        self.blue = {1: False, 2: False}
        self.black = {1: False, 2: False}
        self.vac_seen = False
        self.last_sts = {"m1": 0, "pro": 0}
        self.m1_job = self.pro_job = 0   # job each bridge actually started
        self.load(wafers)

    def load(self, wafers):
        """Stage wafers in the yellow rack: level 1 first, then level 2."""
        self.yellow = {1: None, 2: None}
        for lvl, colour in zip((1, 2), wafers):
            self.yellow[lvl] = colour
        self.blue = {1: False, 2: False}
        self.black = {1: False, 2: False}

    def move_time(self, dist):
        t_acc = self.VEL / self.ACC
        d_acc = self.VEL * t_acc          # accel + decel distance
        if dist <= d_acc:
            return 2 * (dist / self.ACC) ** 0.5
        return dist / self.VEL + t_acc

    def step(self, p: Sequencer):
        # MC_MoveAbsolute: Done stays TRUE while Execute stays TRUE
        if p.Move_Abs_Cmd:
            if not self.prev_exec:
                self.move_t, self.move_from = 0.0, self.pos
            if not self.jammed:
                self.move_t += SCAN
            target = p.Pos_Cmd
            total = self.move_time(abs(target - self.move_from))
            frac = min(self.move_t / total, 1.0) if total > 0 else 1.0
            self.pos = self.move_from + (target - self.move_from) * frac
            p.Abs_Done = frac >= 1.0
        else:
            p.Abs_Done = False
        self.prev_exec = p.Move_Abs_Cmd
        # MC_Home
        if p.Home_Cmd:
            self.home_t += SCAN if not self.jammed else 0.0
            if self.home_t >= self.HOME_TIME:
                p.Homed = True
                self.pos = 0.0
        else:
            self.home_t = 0.0

        p.Carrier_Datum = abs(self.pos) < 1.0
        p.Disk_In_Carrier = self.carrier is not None and abs(self.pos) < 1.0
        p.Yellow_L1 = self.yellow[1] is not None
        p.Yellow_L2 = self.yellow[2] is not None
        p.Blue_L1, p.Blue_L2 = self.blue[1], self.blue[2]
        p.Black_L1, p.Black_L2 = self.black[1], self.black[2]
        p.Vac_Present = p.Vac_On
        p.Sim_Tape = TAPE_CODE.get(self.carrier, 0) if abs(self.pos - 170) < 1 else 0

    def robots(self, p: Sequencer):
        """Apply what the robots would have done, from their bridges' status."""
        m1, pro = p.Status_Word_2, p.Status_Word
        m1_prev, pro_prev = self.last_sts["m1"], self.last_sts["pro"]
        # remember the job a bridge started: after a fault the PLC clears the
        # command, but the robot still finishes the motion it was given
        if m1 in (1, 3, 2) and m1_prev in (0, 99) and p.Cmd_Word_2:
            self.m1_job = p.Cmd_Word_2
        if pro in (1, 3, 2) and pro_prev in (0, 99) and p.Cmd_Word:
            self.pro_job = p.Cmd_Word
        if p.Vac_On and pro in (1, 3):
            self.vac_seen = True
        # M1 Pro pick (job 1/3): wafer on the fork when the job is done
        if m1 == 2 and m1_prev != 2 and self.m1_job in (1, 3):
            lvl = 1 if self.m1_job == 1 else 2
            if self.yellow[lvl] is None:
                log(f"  [sim] M1 Pro reported a pick, but yellow level {lvl} was empty")
            elif self.m1_fork is not None:
                log("  [sim] M1 Pro reported a pick, but it was already holding a wafer")
            else:
                self.m1_fork, self.yellow[lvl] = self.yellow[lvl], None
                log(f"  [sim] wafer ({self.m1_fork}) picked: yellow L{lvl} -> M1 Pro fork")
        # M1 Pro place (job 5): wafer in the carrier once the arm is clear
        if m1 in (3, 2) and m1_prev not in (3, 2) and self.m1_job == 5:
            if self.m1_fork is None:
                log("  [sim] M1 Pro reported a place, but it held no wafer")
            elif abs(self.pos) >= 1.0:
                log("  [sim] M1 Pro placed a wafer, but the carrier was not at the load point"
                    " - wafer dropped")
                self.m1_fork = None
            elif self.carrier is not None:
                log("  [sim] M1 Pro placed a wafer on top of another one - wafer dropped")
                self.m1_fork = None
            else:
                self.carrier, self.m1_fork = self.m1_fork, None
                log(f"  [sim] wafer ({self.carrier}) placed: M1 Pro fork -> carrier")
        # Pro 600 sort (job 1/4): off the carrier at clear, into the rack at done
        if pro in (3, 2) and pro_prev not in (3, 2) and self.pro_job in (1, 4):
            if self.carrier is None or abs(self.pos - 170) >= 1.0:
                log("  [sim] Pro 600 lifted, but there was no wafer at the pick point")
            elif not self.vac_seen:
                log("  [sim] Pro 600 lifted without Vac_On: the wafer stayed in the carrier")
            else:
                self.pro_cup, self.carrier = self.carrier, None
                log(f"  [sim] wafer ({self.pro_cup}) lifted: carrier -> Pro 600 cup")
        if pro == 2 and pro_prev != 2 and self.pro_job in (1, 4) and self.pro_cup is not None:
            rack = self.blue if self.pro_job == 1 else self.black
            name = "blue" if self.pro_job == 1 else "black"
            lvl = 1 if not rack[1] else 2
            if rack[lvl]:
                log(f"  [sim] {name} rack full - wafer dropped")
            else:
                rack[lvl] = True
                log(f"  [sim] wafer ({self.pro_cup}) placed: Pro 600 cup -> {name} L{lvl}")
            self.pro_cup = None
        if pro not in (1, 2, 3):
            self.vac_seen = False
        self.last_sts = {"m1": m1, "pro": pro}


# --------------------------------------------------------------------------
# Minimal Modbus TCP server (FC 1, 3, 5, 6, 15, 16)
# --------------------------------------------------------------------------
class ModbusServer:
    def __init__(self, plc, lock, strict=True, verbose=False):
        self.plc, self.lock, self.strict, self.verbose = plc, lock, strict, verbose
        self.clients = 0

    def _get(self, table, addr):
        name = table.get(addr)
        if name is None:
            return None if self.strict else 0
        return int(getattr(self.plc, name))

    def _set(self, table, addr, value):
        name = table.get(addr)
        if name is None:
            return self.strict is False
        if table is COILS:
            setattr(self.plc, name, bool(value))
        else:
            setattr(self.plc, name, value & 0xFFFF)
        return True

    def handle(self, fc, data):
        """Return response PDU bytes (without function code) or an exception code."""
        with self.lock:
            if fc in (1, 3):
                addr, count = struct.unpack(">HH", data[:4])
                if not 1 <= count <= (2000 if fc == 1 else 125):
                    return 3
                table = COILS if fc == 1 else HOLDING
                vals = [self._get(table, a) for a in range(addr, addr + count)]
                if any(v is None for v in vals):
                    return 2
                if fc == 3:
                    return struct.pack(">B", 2 * count) + b"".join(
                        struct.pack(">H", v) for v in vals)
                out = bytearray((count + 7) // 8)
                for i, v in enumerate(vals):
                    if v:
                        out[i // 8] |= 1 << (i % 8)
                return struct.pack(">B", len(out)) + bytes(out)
            if fc == 5:
                addr, val = struct.unpack(">HH", data[:4])
                if val not in (0x0000, 0xFF00):
                    return 3
                return data[:4] if self._set(COILS, addr, val == 0xFF00) else 2
            if fc == 6:
                addr, val = struct.unpack(">HH", data[:4])
                return data[:4] if self._set(HOLDING, addr, val) else 2
            if fc == 15:
                addr, count, nbytes = struct.unpack(">HHB", data[:5])
                bits = data[5:5 + nbytes]
                if any(COILS.get(a) is None for a in range(addr, addr + count)) and self.strict:
                    return 2
                for i in range(count):
                    self._set(COILS, addr + i, (bits[i // 8] >> (i % 8)) & 1)
                return data[:4]
            if fc == 16:
                addr, count, nbytes = struct.unpack(">HHB", data[:5])
                if any(HOLDING.get(a) is None for a in range(addr, addr + count)) and self.strict:
                    return 2
                for i in range(count):
                    self._set(HOLDING, addr + i, struct.unpack(">H", data[5 + 2 * i:7 + 2 * i])[0])
                return data[:4]
            return 1  # illegal function

    async def serve_client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        self.clients += 1
        log(f"client connected: {peer[0]}:{peer[1]}")
        try:
            while True:
                header = await reader.readexactly(7)
                tid, pid, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                fc, data = pdu[0], pdu[1:]
                res = self.handle(fc, data)
                if isinstance(res, int):
                    if self.verbose:
                        log(f"  exception {res} for FC{fc} from {peer[1]}")
                    body = struct.pack(">BB", fc | 0x80, res)
                else:
                    body = struct.pack(">B", fc) + res
                writer.write(struct.pack(">HHHB", tid, pid, len(body) + 1, unit) + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self.clients -= 1
            log(f"client disconnected: {peer[0]}:{peer[1]}")
            writer.close()


# --------------------------------------------------------------------------
# Scan loop, console and main
# --------------------------------------------------------------------------
def scan_loop(plc, world, lock, stop):
    last_step, last_m1, last_pro, last_fault = None, None, None, False
    next_t = time.monotonic()
    while not stop.is_set():
        with lock:
            world.robots(plc)
            plc.scan()
            world.step(plc)
            if plc.Step != last_step:
                log(f"step {plc.Step:>2}  {STEP_NAMES.get(plc.Step, '')}")
                last_step = plc.Step
            if plc.M1_State != last_m1:
                log(f"          M1 Pro  {plc.M1_State}  {M1_NAMES.get(plc.M1_State, '')}")
                last_m1 = plc.M1_State
            if plc.Pro_State != last_pro:
                log(f"          Pro 600 {plc.Pro_State}  {PRO_NAMES.get(plc.Pro_State, '')}")
                last_pro = plc.Pro_State
            if plc.Fault and not last_fault:
                log(f"FAULT {plc.Fault_Code}: {FAULTS.get(plc.Fault_Code, '?')}"
                    f"  (step {plc.Step}, M1 Pro {plc.M1_State}, Pro 600 {plc.Pro_State})")
            if not plc.Fault and last_fault:
                log("fault cleared")
            last_fault = plc.Fault
        next_t += SCAN
        time.sleep(max(0.0, next_t - time.monotonic()))


HELP = """commands:
  status                 show registers, coils and racks
  start | reset | home   press START / FAULT RESET / HOME
  reload [c1,c2]         stage wafers in the yellow rack, e.g. reload blue,green
                         (colours: blue, green, none) and empty the output racks
  estop | estop off      open / close the E-stop circuit
  jam | unjam            stop / free the conveyor (to test fault 11)
  unload                 take the wafer off the carrier (after a fault)
  set <ref> <value>      write a register or coil, e.g. set 400052 40000
  quit"""


def print_status(plc, world):
    print("  holding: " + "  ".join(
        f"4{a + 1:05d} {n}={int(getattr(plc, n))}" for a, n in HOLDING.items()))
    print("  coils:   " + "  ".join(
        f"{n}={int(getattr(plc, n))}" for n in COILS.values()))
    print(f"  conveyor pos={world.pos:.1f}  carrier={world.carrier}  M1 fork={world.m1_fork}  "
          f"Pro 600 cup={world.pro_cup}  "
          f"yellow={world.yellow}  blue={world.blue}  black={world.black}"
          f"{'  JAMMED' if world.jammed else ''}")


def console(plc, world, lock, stop):
    for line in sys.stdin:
        cmd = line.strip().split()
        if not cmd:
            continue
        with lock:
            c = cmd[0].lower()
            if c == "help":
                print(HELP)
            elif c == "status":
                print_status(plc, world)
            elif c == "start":
                plc.Start_Cmd = True
            elif c == "reset":
                plc.Fault_Reset = True
            elif c == "home":
                plc.Home_Req = True
            elif c == "reload":
                wafers = cmd[1].split(",") if len(cmd) > 1 else ["blue", "green"]
                world.load(wafers)
                log(f"yellow rack loaded: {world.yellow}")
            elif c == "estop":
                plc.EStop_OK = len(cmd) > 1 and cmd[1] == "off"
                log("E-stop " + ("closed (OK)" if plc.EStop_OK else "OPEN"))
            elif c == "unload":
                if world.carrier is None:
                    print("the carrier is empty")
                else:
                    log(f"operator removed the wafer ({world.carrier}) from the carrier")
                    world.carrier = None
            elif c == "set" and len(cmd) == 3:
                ref, val = cmd[1], cmd[2]
                try:
                    num, v = int(ref), int(val)
                except ValueError:
                    print("usage: set 400052 40000   or   set 000007 1")
                    continue
                if ref.startswith("4"):
                    name = HOLDING.get(num - 400001)
                    v &= 0xFFFF
                else:
                    name = COILS.get(num - 1)
                    v = bool(v)
                if name is None:
                    print(f"{ref} is not in the register map")
                else:
                    setattr(plc, name, v)
                    log(f"{ref} {name} = {int(v)}  (the PLC or sensors may overwrite it)")
            elif c in ("jam", "unjam"):
                world.jammed = c == "jam"
                log("conveyor " + ("jammed" if world.jammed else "free"))
            elif c in ("quit", "exit"):
                stop.set()
                return
            else:
                print("unknown command - type help")


async def amain(args):
    plc = Sequencer()
    world = World(args.wafers.split(","))
    lock = threading.Lock()
    stop = threading.Event()
    threading.Thread(target=scan_loop, args=(plc, world, lock, stop), daemon=True).start()
    if not args.no_console:
        threading.Thread(target=console, args=(plc, world, lock, stop), daemon=True).start()
    server = ModbusServer(plc, lock, strict=not args.lenient, verbose=args.verbose)
    srv = await asyncio.start_server(server.serve_client, args.host, args.port)
    log(f"PLC simulator listening on {args.host}:{args.port}  "
        f"(yellow rack: {world.yellow})  - type 'help' for commands")
    async with srv:
        while not stop.is_set():
            await asyncio.sleep(0.2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to listen on (0.0.0.0 to accept other machines)")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--wafers", default="blue,green",
                    help="tape colours staged on yellow L1,L2 (blue/green/none)")
    ap.add_argument("--lenient", action="store_true",
                    help="allow reads/writes to unmapped addresses (returns 0)")
    ap.add_argument("--verbose", action="store_true", help="log Modbus exceptions")
    ap.add_argument("--no-console", action="store_true", help="disable the command prompt")
    args = ap.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
