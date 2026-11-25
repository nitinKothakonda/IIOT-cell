"""OEE dashboard (application layer). Reads the cloud database, refreshes every 3 s.

  pip install -r requirements.txt
  set DATABASE_URL (same as cloud/.env), then:  python app.py   ->  http://localhost:5000
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
from flask import Flask, Response, abort, jsonify, render_template, request, send_file

for env in (Path(__file__).with_name(".env"), Path(__file__).parent.parent / "cloud" / ".env"):
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent.parent / "cloud"))
import export_data as ex   # the same code the command-line export uses (needs openpyxl for .xlsx)

app = Flask(__name__)

RANGES = {"all": (None, "All data"), "1h": (timedelta(hours=1), "Last hour"),
          "24h": (timedelta(hours=24), "Last 24 hours")}


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


# PLC fault codes (same list as FAULTS in edge/cell_reader.py)
FAULT_TEXT = {1: "a device reported status 99", 2: "yellow rack empty", 3: "wafer not in carrier",
              4: "vision found no tape", 5: "carrier did not return", 6: "M1 Pro timeout",
              7: "M1 Pro status stuck", 8: "Pro 600 timeout", 9: "Pro 600 status stuck",
              10: "no vision result", 11: "conveyor timeout", 12: "device not ready",
              13: "E-stop opened", 14: "device went idle without done", 15: "carrier not empty"}


def annotate(stops):
    """Add code / cause / source to each stoppage from cloud/fault_annotations.json (time ranges with a known fault code)."""
    f = Path(__file__).parent.parent / "cloud" / "fault_annotations.json"
    notes = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    t = datetime.fromisoformat
    for s in stops:
        s["code"], s["cause"], s["source"] = None, "not recorded", ""
        for n in notes:
            if t(n["from_utc"]) <= t(s["start_utc"]) <= t(n["to_utc"]):
                s["code"], s["cause"] = n["code"], FAULT_TEXT.get(n["code"], "unknown code")
                s["source"] = n.get("source", "")
                break
    return stops


def load_range():
    delta, label = RANGES.get(request.args.get("range", "all"), RANGES["all"])
    since = datetime.now(timezone.utc) - delta if delta else None
    tables = ex.load(since)
    tables["downtime"] = annotate(tables["downtime"])
    return tables, label


@app.get("/faults")
def faults_page():
    return render_template("faults.html")


@app.get("/api/faults")
def faults_api():
    tables, label = load_range()
    stops, oees = tables["downtime"], tables["oee_snapshots"]
    last = oees[-1] if oees else {}
    n, total = len(stops), sum(x["seconds"] for x in stops)
    by = {}
    for x in stops:
        k = f'code {x["code"]}: {x["cause"]}' if x["code"] is not None else "cause not recorded"
        b = by.setdefault(k, {"cause": k, "stops": 0, "seconds": 0.0})
        b["stops"] += 1
        b["seconds"] = round(b["seconds"] + x["seconds"], 1)
    run, down = last.get("run_s"), last.get("down_s")
    stats = {"stops": n, "total_s": round(total, 1), "mean_s": round(total / n, 1) if n else None,
             "longest_s": max((x["seconds"] for x in stops), default=None),
             "mtbf_s": round(run / n, 1) if n and run else None,
             "down_share": round(down / (run + down), 3) if run is not None and down is not None and (run + down) else None}
    return jsonify(range=label, stops=stops, by_cause=sorted(by.values(), key=lambda b: -b["seconds"]), stats=stats)


@app.get("/export/<fmt>")
def export(fmt):
    """Download the collected data: xlsx (all sheets), json (all tables) or csv (wafers)."""
    delta, label = RANGES.get(request.args.get("range", "all"), RANGES["all"])
    since = datetime.now(timezone.utc) - delta if delta else None
    tables = ex.load(since)
    tables["downtime"] = annotate(tables["downtime"])
    name = "wafer-cell-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        payload = {"exported_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "range": label,
                   "summary": dict(ex.summary(tables["wafer_events"], tables["oee_snapshots"], label)), **tables}
        return Response(json.dumps(payload, indent=2, default=str), mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename={name}.json"})
    if fmt == "faults":
        rows = tables["downtime"]
        buf = io.StringIO()
        cols = ["stop_no", "start_local", "end_local", "seconds", "code", "cause", "source", "start_utc", "end_utc"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}-faults.csv"})
    if fmt == "csv":
        rows = tables["wafer_events"]
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}-wafers.csv"})
    if fmt == "xlsx":
        wb = ex.build_workbook(tables, label)
        if wb is None:
            abort(501, "Excel export needs openpyxl:  pip install openpyxl")
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=name + ".xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    abort(404)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
