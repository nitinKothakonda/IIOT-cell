"""OEE = Availability x Performance x Quality, from run/down time and wafer counts."""
from __future__ import annotations


def compute(run_s: float, down_s: float, parts: int, good: int, ideal_cycle_s: float) -> dict:
    planned = run_s + down_s
    availability = run_s / planned if planned else 0.0
    performance = min(1.0, parts * ideal_cycle_s / run_s) if run_s else 0.0
    quality = good / parts if parts else 0.0
    return {
        "availability": round(availability, 4),
        "performance": round(performance, 4),
        "quality": round(quality, 4),
        "oee": round(availability * performance * quality, 4),
        "run_s": run_s,
        "down_s": down_s,
        "parts": parts,
        "good": good,
        "reject": parts - good,
    }
