"""OEE dashboard (application layer). Reads the cloud database, refreshes every 3 s.

  pip install -r requirements.txt
  set DATABASE_URL (same as cloud/.env), then:  python app.py   ->  http://localhost:5000
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, render_template

for env in (Path(__file__).with_name(".env"), Path(__file__).parent.parent / "cloud" / ".env"):
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

app = Flask(__name__)


def query(q, *args):
    with psycopg2.connect(os.environ["DATABASE_URL"]) as db, \
            db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(q, args)
        return [dict(r) for r in cur.fetchall()]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/summary")
def summary():
    latest = query("select * from oee_snapshots order by ts desc limit 1")
    trend = query("""select ts, oee, availability, performance, quality from (
                       select * from oee_snapshots order by ts desc limit 150) t order by ts""")
    parts = query("""select ts_end as ts, wafer_id, arm, station, cycle_s, label, confidence, result
                     from wafer_events order by ts_end desc limit 15""")
    mix = query("select label, result, count(*) n from wafer_events group by label, result order by n desc")
    arms = query("""select arm, count(*) n, round(avg(cycle_s)::numeric,2) avg_cycle,
                    round((100.0*sum((result='defect')::int)/count(*))::numeric,1) defect_pct
                    from wafer_events group by arm order by arm""")
    alerts = query("select ts, kind, message, cleared from alerts order by ts desc limit 10")
    edge = query("select online, last_seen, extract(epoch from now()-last_seen) age_s from edge_status limit 1")
    return jsonify(latest=latest[0] if latest else None, trend=trend, parts=parts, mix=mix, arms=arms,
                   alerts=alerts, edge=edge[0] if edge else None)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
