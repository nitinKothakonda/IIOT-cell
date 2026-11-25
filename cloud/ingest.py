"""Cloud ingest: MQTT (Mosquitto) -> PostgreSQL (local PostgreSQL), plus simple alerts.

Subscribes to  <prefix>/wafer, <prefix>/oee, <prefix>/status  (JSON from the edge script).

  pip install -r requirements.txt
  copy .env.example to .env and fill it in
  python ingest.py
"""
from __future__ import annotations

import json
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
import psycopg2


def load_env(path: Path):
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env(Path(__file__).with_name(".env"))
E = os.environ
PREFIX = E.get("TOPIC_PREFIX", "mfg598/group3/wafer")
CELL = PREFIX.rstrip("/").split("/")[-1]
QUALITY_MIN = float(E.get("QUALITY_ALERT_BELOW", "0.6"))
STOPPED_MAX_S = float(E.get("STOPPED_ALERT_AFTER_S", "60"))

def connect_db():
    conn = psycopg2.connect(E["DATABASE_URL"])
    conn.autocommit = True
    return conn


db = connect_db()
stopped_since: float | None = None


def sql(q, *args):
    with db.cursor() as cur:
        cur.execute(q, args)


def ts_of(msg: dict):
    return msg.get("time") or datetime.now(timezone.utc).isoformat()


def raise_alert(kind: str, text: str):
    with db.cursor() as cur:
        cur.execute("select 1 from alerts where cell=%s and kind=%s and cleared is null", (CELL, kind))
        if cur.fetchone():
            return
        cur.execute("insert into alerts (cell, kind, message) values (%s,%s,%s)", (CELL, kind, text))
    print("ALERT", kind, "-", text)


def clear_alert(kind: str):
    sql("update alerts set cleared=now() where cell=%s and kind=%s and cleared is null", CELL, kind)


def on_wafer(m: dict):
    sql("""insert into wafer_events (cell, wafer_id, ts_start, ts_end, arm, station, cycle_s, label, confidence, result)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        CELL, m["wafer_id"], m["ts_start"], m["ts_end"], m["arm"], m.get("station", "handoff"),
        m["cycle_s"], m.get("label"), m.get("confidence"), m.get("result", "defect"))


def on_oee(m: dict):
    global stopped_since
    sql("""insert into oee_snapshots (cell, ts, availability, performance, quality, oee,
                                      run_s, down_s, parts, good, reject, running)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        CELL, ts_of(m), m["availability"], m["performance"], m["quality"], m["oee"],
        m["run_s"], m["down_s"], m["parts"], m["good"], m["reject"], m.get("running"))
    sql("""insert into edge_status (cell, online, last_seen) values (%s, true, now())
           on conflict (cell) do update set online=true, last_seen=now()""", CELL)
    clear_alert("edge_offline")

    if m["parts"] >= 5 and m["quality"] < QUALITY_MIN:
        raise_alert("quality_low", f"Quality {m['quality']:.0%} is below {QUALITY_MIN:.0%}")
    elif m["quality"] >= QUALITY_MIN:
        clear_alert("quality_low")

    if not m.get("running"):
        stopped_since = stopped_since or time.time()
        if time.time() - stopped_since > STOPPED_MAX_S:
            raise_alert("stopped", f"Cell stopped for more than {STOPPED_MAX_S:.0f} s")
    else:
        stopped_since = None
        clear_alert("stopped")


def on_status(m: dict):
    sql("""insert into edge_status (cell, online, last_seen) values (%s, %s, now())
           on conflict (cell) do update set online=excluded.online, last_seen=now()""",
        CELL, bool(m.get("online")))
    if m.get("online"):
        clear_alert("edge_offline")
    else:
        raise_alert("edge_offline", "Edge PC went offline")


HANDLERS = {"wafer": on_wafer, "oee": on_oee, "status": on_status}


def on_message(client, userdata, msg):
    global db
    kind = msg.topic.rsplit("/", 1)[-1]
    if kind not in HANDLERS:
        return
    for attempt in (1, 2):
        try:
            HANDLERS[kind](json.loads(msg.payload))
            return
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            if attempt == 2:
                print(f"Database unreachable, dropped {msg.topic}: {e}")
            else:                       # connection to PostgreSQL dropped: reconnect once and retry
                print("Database connection lost, reconnecting...")
                try:
                    db = connect_db()
                except Exception:
                    time.sleep(2)
        except Exception as e:          # bad message: skip it, keep running
            print(f"Skipped {msg.topic}: {e}")
            return


def on_connect(client, userdata, flags, rc, props=None):
    print(f"Connected to broker (rc={rc}); listening on {PREFIX}/#")
    client.subscribe(f"{PREFIX}/#", qos=1)


def main():
    # Fixed client id + clean_session=False = persistent session: while this script is off,
    # the broker keeps the QoS 1 messages and delivers them when it reconnects (store-and-forward).
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mfg598-cloud-ingest", clean_session=False)
    if E.get("MQTT_USER"):
        c.username_pw_set(E["MQTT_USER"], E.get("MQTT_PASS", ""))
    if E.get("MQTT_TLS", "false").lower() == "true":
        c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    c.on_connect, c.on_message = on_connect, on_message
    c.connect(E["MQTT_HOST"], int(E.get("MQTT_PORT", "1883")))
    c.loop_forever()


if __name__ == "__main__":
    main()
