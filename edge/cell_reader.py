"""Read-only reader for the wafer cell PLC (Micro850, Modbus TCP) -> MQTT.

Replaces edge/wafer_sim.py with real data. It only READS: Modbus function codes
1 (coils) and 3 (holding registers) are used, never a write, so it cannot
disturb the cell or the lab bridges.

What it turns into events (register map: lab/plc_sim.py, MbSrvConf.xml):
  Pass_Count (400052) / Reject_Count (400053) rise  -> the vision verdict for a wafer
  Step (400031) reaches 11 afterwards               -> wafer sorted, arm clear -> publish /wafer
  Fault (coil 5) / Fault_Code (400051)              -> down time and an alert
  Step 0 = idle (not counted as run or down time)

  python cell_reader.py --sim --dry-run                 # against lab/plc_sim.py, print only
  python cell_reader.py --host 192.168.10.10 --user edge --password <pw>   # real cell
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone

from pymodbus.client import ModbusTcpClient

from oee import compute

PREFIX = "mfg598/group3/wafer"
FAULTS = {1: "a device reported status 99", 2: "yellow rack empty", 3: "wafer not in carrier",
          4: "vision found no tape", 5: "carrier did not return", 6: "M1 Pro timeout",
          7: "M1 Pro status stuck", 8: "Pro 600 timeout", 9: "Pro 600 status stuck",
          10: "no vision result", 11: "conveyor timeout", 12: "device not ready",
          13: "E-stop opened", 14: "device went idle without done", 15: "carrier not empty"}
STEP_SORTED = 11   # wafer lifted by the Pro 600, carrier returning


def read_cell(plc) -> dict | None:
    """One read-only snapshot, or None if any read fails. Only mapped addresses are read."""
    coils = plc.read_coils(0, count=17)
    st = plc.read_holding_registers(30, count=3)      # step, M1 state, Pro state
    vis = plc.read_holding_registers(40, count=1)
    cnt = plc.read_holding_registers(50, count=3)     # fault code, pass, reject
    hb = plc.read_holding_registers(60, count=1)
    if any(r.isError() for r in (coils, st, vis, cnt, hb)):
        return None
    return {"fault": bool(coils.bits[4]), "estop_ok": bool(coils.bits[5]),
            "step": st.registers[0], "m1": st.registers[1], "pro": st.registers[2],
            "vision": vis.registers[0], "fault_code": cnt.registers[0],
            "passed": cnt.registers[1], "rejected": cnt.registers[2], "heartbeat": hb.registers[0]}


class Tracker:
    """Turns successive snapshots into wafer events and run/down time."""

    def __init__(self, ideal_cycle_s):
        self.ideal = ideal_cycle_s
        self.prev = None
        self.run_s = self.down_s = 0.0
        self.parts = self.good = 0
        self.pending = None          # verdict waiting for the wafer to be sorted
        self.t_load = None           # when the wafer was placed in the carrier
        self.last_t = None
        self.out = []                # (topic suffix, payload) to publish

    def update(self, s, now):
        dt = 0.0 if self.last_t is None else min(now - self.last_t, 5.0)
        self.last_t = now
        p = self.prev
        if s["fault"]:
            self.down_s += dt
        elif s["step"] != 0:
            self.run_s += dt
        if p is not None:
            if s["step"] in (2, 6) and p["step"] not in (2, 6):   # step 2 is short; 6 follows it
                self.t_load = now
            if s["passed"] != p["passed"] or s["rejected"] != p["rejected"]:
                good = s["passed"] != p["passed"]
                self.pending = {"good": good, "ts_start": self.t_load or now}
            if self.pending and s["step"] == STEP_SORTED and p["step"] != STEP_SORTED:
                self._emit(now)
            if s["fault"] and not p["fault"]:
                self.out.append(("alert", {"kind": "plc_fault", "code": s["fault_code"],
                                           "text": FAULTS.get(s["fault_code"], "?")}))
        self.prev = s

    def _emit(self, now):
        v, self.pending = self.pending, None
        self.parts += 1
        self.good += v["good"]
        cycle = round(now - v["ts_start"], 2)
        n = self.prev["passed"] + self.prev["rejected"]
        wall = datetime.now(timezone.utc)
        self.out.append(("wafer", {
            "wafer_id": f"W{n:05d}", "arm": "pro600", "station": "blue_rack" if v["good"] else "black_rack",
            "ts_start": (wall - timedelta(seconds=cycle)).isoformat(), "ts_end": wall.isoformat(),
            "cycle_s": cycle, "label": "blue_tape" if v["good"] else "green_tape",
            "confidence": 1.0, "result": "good" if v["good"] else "defect"}))
        self.out.append(("oee", self.oee_msg()))

    def oee_msg(self):
        run = self.prev is not None and not self.prev["fault"] and self.prev["step"] != 0
        return {**compute(self.run_s, self.down_s, self.parts, self.good, self.ideal),
                "running": run, "time": datetime.now(timezone.utc).isoformat()}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sim", action="store_true", help="use lab/plc_sim.py on 127.0.0.1:5020")
    ap.add_argument("--host"); ap.add_argument("--port", type=int)
    ap.add_argument("--mqtt-host", default="localhost"); ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--user", default="edge"); ap.add_argument("--password", default="change-me")
    ap.add_argument("--ideal-cycle", type=float, default=30.0,
                    help="ideal seconds per wafer for the Performance figure (measure it in the lab)")
    ap.add_argument("--poll", type=float, default=0.2)
    ap.add_argument("--dry-run", action="store_true", help="print instead of publishing")
    a = ap.parse_args()
    host = a.host or ("127.0.0.1" if a.sim else "192.168.10.10")
    port = a.port or (5020 if a.sim else 502)

    plc = ModbusTcpClient(host, port=port)
    if not plc.connect():
        raise SystemExit(f"could not reach the PLC at {host}:{port}")
    print(f"PLC connected at {host}:{port} (read-only)")

    c = None
    if not a.dry_run:
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="wafer-edge-cell")
        c.username_pw_set(a.user, a.password)
        c.will_set(f"{PREFIX}/status", json.dumps({"online": False}), qos=1, retain=True)
        c.connect(a.mqtt_host, a.mqtt_port)
        c.loop_start()
        c.publish(f"{PREFIX}/status", json.dumps({"online": True}), qos=1, retain=True)

    t = Tracker(a.ideal_cycle)
    last_oee = 0.0
    try:
        while True:
            s = None
            try:
                s = read_cell(plc)
            except Exception as e:
                print("PLC read problem:", e)
                time.sleep(1.0); plc.connect()
            now = time.monotonic()
            if s:
                t.update(s, now)
                if now - last_oee > 5 and t.prev:
                    t.out.append(("oee", t.oee_msg())); last_oee = now
            for topic, payload in t.out:
                if topic == "alert":
                    print("ALERT", payload)
                    continue
                print(topic, json.dumps(payload) if a.dry_run or topic == "wafer" else "")
                if c:
                    c.publish(f"{PREFIX}/{topic}", json.dumps(payload), qos=1)
            t.out.clear()
            time.sleep(a.poll)
    except KeyboardInterrupt:
        pass
    if c:
        c.publish(f"{PREFIX}/status", json.dumps({"online": False}), qos=1, retain=True).wait_for_publish()
        c.loop_stop()
    plc.close()


if __name__ == "__main__":
    main()
