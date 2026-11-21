"""Fake two-arm wafer cell. Publishes the same MQTT messages the real edge will publish on Monday.

Topics (prefix mfg598/group3/wafer):
  /wafer   one JSON per wafer: wafer_id, arm, station, ts_start, ts_end, cycle_s, label, confidence, result
  /oee     rolling OEE every few seconds
  /status  online / offline (last will)

Replace read_event() with the real robot/camera reader on Monday; everything else stays the same.

  python wafer_sim.py --host localhost --user edge --password change-me --speed 5
  python wafer_sim.py --burst          # makes defects spike, to trigger the quality alert
"""
from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt

from oee import compute

PREFIX = "mfg598/group3/wafer"
IDEAL_CYCLE_S = 8.0
LABELS = [("ok", True), ("scratch", False), ("chip", False), ("misaligned", False)]


def read_event(n: int, burst: bool) -> dict:
    """One wafer through the handoff. Real version: arm signals + camera model result."""
    arm = random.choice(["arm_a", "arm_b"])
    cycle = max(6.0, random.gauss(9.0 if arm == "arm_a" else 9.6, 1.0))
    p_defect = 0.55 if burst else 0.07
    if random.random() < p_defect:
        label = random.choice(["scratch", "chip", "misaligned"])
    else:
        label = "ok"
    return {"wafer_id": f"W{n:05d}", "arm": arm, "station": "handoff", "cycle_s": round(cycle, 2),
            "label": label, "confidence": round(random.uniform(0.82, 0.99), 3),
            "result": "good" if label == "ok" else "defect"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--user", default="edge")
    ap.add_argument("--password", default="change-me")
    ap.add_argument("--speed", type=float, default=5.0, help="x faster than real time")
    ap.add_argument("--burst", action="store_true", help="high defect rate")
    ap.add_argument("--count", type=int, default=0, help="stop after N wafers (0 = forever)")
    a = ap.parse_args()

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="wafer-edge-sim")
    c.username_pw_set(a.user, a.password)
    c.will_set(f"{PREFIX}/status", json.dumps({"online": False}), qos=1, retain=True)
    c.connect(a.host, a.port)
    c.loop_start()
    c.publish(f"{PREFIX}/status", json.dumps({"online": True}), qos=1, retain=True)

    run_s = down_s = 0.0
    parts = good = 0
    n = 0
    try:
        while a.count == 0 or n < a.count:
            n += 1
            # occasional stoppage (jam / operator): 4% of wafers, 20-60 s
            if random.random() < 0.04:
                stop = random.uniform(20, 60)
                down_s += stop
                c.publish(f"{PREFIX}/oee", json.dumps({**compute(run_s, down_s, parts, good, IDEAL_CYCLE_S),
                          "running": False, "time": datetime.now(timezone.utc).isoformat()}), qos=1)
                time.sleep(min(3.0, stop / a.speed))
            ev = read_event(n, a.burst)
            end = datetime.now(timezone.utc)
            ev["ts_start"], ev["ts_end"] = (end - timedelta(seconds=ev["cycle_s"] / a.speed)).isoformat(), end.isoformat()
            run_s += ev["cycle_s"]
            parts += 1
            good += ev["result"] == "good"
            c.publish(f"{PREFIX}/wafer", json.dumps(ev), qos=1)
            c.publish(f"{PREFIX}/oee", json.dumps({**compute(run_s, down_s, parts, good, IDEAL_CYCLE_S),
                      "running": True, "time": datetime.now(timezone.utc).isoformat()}), qos=1)
            time.sleep(ev["cycle_s"] / a.speed)
    except KeyboardInterrupt:
        pass
    c.publish(f"{PREFIX}/status", json.dumps({"online": False}), qos=1, retain=True).wait_for_publish()
    c.loop_stop()


if __name__ == "__main__":
    main()
