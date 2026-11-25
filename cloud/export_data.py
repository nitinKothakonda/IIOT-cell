"""Export the collected cell data from PostgreSQL to JSON and Excel.

Used two ways:
  - from the command line:   cd cloud ; python export_data.py
  - by the dashboard's Export menu (dashboard/app.py imports load() and build_workbook())

    pip install openpyxl            (once; JSON works without it)

Reads DATABASE_URL from cloud/.env (same file ingest.py uses). The command line
writes into ../exports/ :
    wafer_events.json, oee_snapshots.json, alerts.json, downtime.json
    wafer_cell_data.xlsx   (sheets: Summary, Wafers, OEE_snapshots, Alerts, Downtime)

Times are exported twice: ISO in UTC and a readable local time (this PC's
time zone, the same one the dashboard shows).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "exports"

# name -> (select, time column used for ordering and for the time range)
QUERIES = {
    "wafer_events": ("select id, cell, wafer_id, ts_start, ts_end, arm, station, cycle_s, label, confidence, result "
                     "from wafer_events", "ts_end"),
    "oee_snapshots": ("select id, cell, ts, availability, performance, quality, oee, run_s, down_s, parts, good, reject, running "
                      "from oee_snapshots", "ts"),
    "alerts": ("select id, cell, ts, kind, message, cleared from alerts", "ts"),
}


def load_env(path: Path):
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def fetch(since=None):
    """Raw rows from the database; `since` (a timezone-aware datetime) limits them to a time range."""
    with psycopg2.connect(os.environ["DATABASE_URL"]) as db, \
         db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        data = {}
        for name, (sql, col) in QUERIES.items():
            args = ()
            if since is not None:
                sql += f" where {col} >= %s"
                args = (since,)
            cur.execute(sql + f" order by {col}", args)
            data[name] = [dict(r) for r in cur.fetchall()]
    return data


def clean(rows):
    """JSON/Excel friendly rows: times as UTC ISO plus a local readable column."""
    out = []
    for r in rows:
        n = {}
        for k, v in r.items():
            if isinstance(v, datetime):
                n[k + "_utc"] = v.astimezone(timezone.utc).isoformat()
                n[k + "_local"] = v.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            else:
                n[k] = float(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, (int, bool)) else v
        out.append(n)
    return out


def downtime_intervals(oees):
    """Stoppages rebuilt from the OEE history: the down-time counter rises while the cell is faulted."""
    out, cur = [], None
    for a, b in zip(oees, oees[1:]):
        grew = (b.get("down_s") or 0) - (a.get("down_s") or 0)
        if grew > 0.5:
            if cur is None:
                cur = {"start_utc": a["ts_utc"], "start_local": a["ts_local"], "seconds": 0.0}
            cur["end_utc"], cur["end_local"] = b["ts_utc"], b["ts_local"]
            cur["seconds"] += grew
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return [{"stop_no": i, "start_local": r["start_local"], "end_local": r["end_local"],
             "seconds": round(r["seconds"], 1), "start_utc": r["start_utc"], "end_utc": r["end_utc"]}
            for i, r in enumerate(out, 1)]


def load(since=None):
    """Everything the exports need, already cleaned."""
    tables = {k: clean(v) for k, v in fetch(since).items()}
    tables["downtime"] = downtime_intervals(tables["oee_snapshots"])
    return tables


def summary(wafers, oees, label="All data"):
    good = sum(1 for w in wafers if w["result"] == "good")
    n = len(wafers)
    cyc = [w["cycle_s"] for w in wafers if w.get("cycle_s") is not None]
    last = oees[-1] if oees else {}
    return [
        ("Exported (local)", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Range", label),
        ("Wafers", n),
        ("Good", good),
        ("Defect", n - good),
        ("Quality (good / all)", round(good / n, 3) if n else None),
        ("Average cycle (s)", round(sum(cyc) / len(cyc), 2) if cyc else None),
        ("Shortest / longest cycle (s)", f"{min(cyc):.2f} / {max(cyc):.2f}" if cyc else None),
        ("First wafer (local)", wafers[0]["ts_end_local"] if n else None),
        ("Last wafer (local)", wafers[-1]["ts_end_local"] if n else None),
        ("Last OEE", last.get("oee")),
        ("Last availability", last.get("availability")),
        ("Last performance", last.get("performance")),
        ("Last quality", last.get("quality")),
        ("Run time (s)", last.get("run_s")),
        ("Down time (s)", last.get("down_s")),
    ]


def write_json(tables):
    OUT.mkdir(exist_ok=True)
    for name, rows in tables.items():
        (OUT / f"{name}.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")


def build_workbook(tables, label="All data"):
    """An openpyxl workbook with Summary, Wafers, OEE_snapshots, Alerts and Downtime, or None without openpyxl."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        return None
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Metric", "Value"])
    for row in summary(tables["wafer_events"], tables["oee_snapshots"], label):
        ws.append(list(row))
    sheets = {"Wafers": "wafer_events", "OEE_snapshots": "oee_snapshots", "Alerts": "alerts",
              "Downtime": "downtime"}
    for title, key in sheets.items():
        s = wb.create_sheet(title)
        rows = tables[key]
        if rows:
            cols = list(rows[0].keys())
            s.append(cols)
            for r in rows:
                s.append([r.get(c) for c in cols])
    for s in wb.worksheets:
        for c in s[1]:
            c.font = Font(bold=True)
        for col in s.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            s.column_dimensions[col[0].column_letter].width = min(width + 2, 45)
        s.freeze_panes = "A2"
    return wb


def write_excel(tables):
    wb = build_workbook(tables)
    if wb is None:
        print("openpyxl is not installed: JSON written, Excel skipped (pip install openpyxl)")
        return None
    OUT.mkdir(exist_ok=True)
    path = OUT / "wafer_cell_data.xlsx"
    wb.save(path)
    return path


def main():
    load_env(HERE / ".env")
    tables = load()
    write_json(tables)
    xlsx = write_excel(tables)
    for k, v in tables.items():
        print(f"{k}: {len(v)} rows")
    print("written to", OUT, "(and", xlsx.name + ")" if xlsx else ")")


if __name__ == "__main__":
    main()
