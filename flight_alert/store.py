"""JSON state / history / latest-result files (kept on the repo's `data` branch)."""

from __future__ import annotations

import json
import os
import statistics
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(os.environ.get("FLIGHT_ALERT_DATA", "data"))
HISTORY_DAYS = 120


def load(name: str, default):
    p = DATA_DIR / name
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(name: str, obj) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)


def new_state() -> dict:
    return {
        "version": 1,
        "last_scan": None,
        "last_scan_ok": None,
        "fail_streak": 0,
        "health_alert_at": None,
        "digest_last_date": None,
        "notified": {},
        "routes": {},
        "verify_cache": {},
        "promo_seen": {},
        "promo_pending": [],
        "promo_recent": [],
        "scan_log": [],
    }


def load_state() -> dict:
    state = new_state()
    state.update(load("state.json", {}))
    return state


# ---- price history (per route, one row per day) -------------------------------------
def record_day(history: dict, route: str, day: date, prices_pp: list[float]) -> None:
    if not prices_pp:
        return
    row = history.setdefault(route, {})
    today = {"min": round(min(prices_pp)), "med": round(statistics.median(prices_pp))}
    prev = row.get(day.isoformat())
    if prev:  # keep the day's lowest minimum; median follows the latest scan
        today["min"] = min(today["min"], prev.get("min", today["min"]))
    row[day.isoformat()] = today
    cutoff = (day - timedelta(days=HISTORY_DAYS)).isoformat()
    for k in [k for k in row if k < cutoff]:
        del row[k]


def route_average(history: dict, route: str, day: date, window: int = 30) -> float | None:
    """Average of the daily median prices over the previous `window` days (falls back to today)."""
    row = history.get(route) or {}
    start = (day - timedelta(days=window)).isoformat()
    past = [v["med"] for k, v in row.items() if start <= k < day.isoformat() and v.get("med")]
    if past:
        return sum(past) / len(past)
    today = row.get(day.isoformat())
    return float(today["med"]) if today and today.get("med") else None
