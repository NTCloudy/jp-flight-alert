"""Separate low-cost-carrier (LCC) alert.

Google Flights cannot price checked bags for us (its bag filter does not change the fares returned for
Taiwan / TWD searches), so LCC fares are fetched bare and an estimated 20 kg checked bag per person,
each way, is added from a per-airline price table (config.lcc.bag_fees). A deal qualifies when
fare + estimated bags < lcc.max_total per person (round trip).

The LCC scan reuses scanner.run() with an LCC-only view of the config and keeps everything in its
own files (state_lcc.json, calendar_lcc.json, history_lcc.json, latest_lcc.json), so the
full-service alert, its state and its emails are untouched. It runs after the full-service scan,
with its own request budget, and never while Google is rate-limiting us.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime

from . import emails, promo, scanner, store
from .config import Config

STATE_FILE = "state_lcc.json"
CALENDAR_FILE = "calendar_lcc.json"
HISTORY_FILE = "history_lcc.json"
LATEST_FILE = "latest_lcc.json"


def view(cfg: Config) -> Config:
    """Same routes, dates, time windows and passengers; LCC airlines only. Prices passed to the
    scanner are bare fares, so its 'interesting' ceiling is max_total minus the cheapest possible
    bags (deals are re-classified on fare + bags afterwards)."""
    lcc = cfg.lcc
    ceiling = lcc.max_total - lcc.min_bag_round_trip
    return dataclasses.replace(cfg, airlines=list(lcc.airlines), instant_max=ceiling, digest_max=ceiling,
                               anomaly_pct=100, renotify_drop=lcc.renotify_drop)


def bag_pp(deal: dict, cfg: Config) -> int:
    """Estimated 20 kg checked bag, per person, outbound + return."""
    return cfg.lcc.bag_fee(deal["out"]["airline"]) + cfg.lcc.bag_fee(deal["back"]["airline"])


def classify(deals: list[dict], cfg: Config) -> list[dict]:
    """Add bag_pp / total_pp; category 'instant' only when fare + bags < max_total."""
    out = []
    for d in deals:
        bags = bag_pp(d, cfg)
        total = d["price_pp"] + bags
        out.append({**d, "bag_pp": bags, "total_pp": total,
                    "category": "instant" if total < cfg.lcc.max_total else None})
    out.sort(key=lambda d: (d["total_pp"], d["dep"]))
    return out


def _sync_cooldown(src: dict, dst: dict) -> None:
    for k in ("cooldown_until", "rate_limit_streak"):
        if src.get(k) is not None:
            dst[k] = src[k]


PROMO_EVERY_HOURS = 0.9


def check_promos(state: dict, now: datetime, dry_run: bool, paused: bool, send, fetch=None) -> list[dict]:
    """LCC sale news (Google News RSS): new headlines are emailed right away in their own email."""
    last = state.get("promo_checked_at")
    if last and (now - datetime.fromisoformat(last)).total_seconds() / 3600 < PROMO_EVERY_HOURS:
        return []
    try:
        items = (fetch or (lambda: promo.fetch(queries=promo.LCC_QUERIES)))()
    except Exception as e:  # noqa: BLE001
        print(f"[廉航促銷] 讀取失敗：{type(e).__name__}: {e}")
        return []
    state["promo_checked_at"] = now.isoformat(timespec="minutes")
    if not state.get("promo_initialized"):
        # first run: remember everything already out there instead of emailing old news
        old = promo.select(items, state["promo_seen"], now, airlines=promo.LCC_AIRLINES, per_airline=10**6)
        promo.update_state(state, old, now)
        state["promo_initialized"] = True
        state["promo_pending"] = []
        print(f"[廉航促銷] 第一次執行：記下現有 {len(old)} 則，之後有新的才寄")
        return []
    new = promo.select(items, state["promo_seen"], now, recent=state.get("promo_recent"),
                       airlines=promo.LCC_AIRLINES)
    print(f"[廉航促銷] 新消息 {len(new)} 則")
    if not new:
        return []
    if paused:
        return []
    subject, text, html = emails.lcc_promos(new)
    if send and send(state, subject, text, html, dry_run):
        promo.update_state(state, new, now)
        state["promo_pending"] = []
        return new
    return []


def run(cfg: Config, source, main_state: dict, now: datetime, dry_run: bool, paused: bool,
        max_requests: int | None = None, send=None, pick=None, scan: bool = True,
        fetch_promos=None) -> scanner.RunResult | None:
    """One LCC pass: sale news, then (when `scan`) fares. `send(state, subject, text, html, dry_run)` and
    `pick(deals, state, cfg, now)` come from app.py (same mail delivery and de-duplication as the
    full-service alert)."""
    if not cfg.lcc.enabled:
        return None
    lcfg = view(cfg)
    state = store.load_state(STATE_FILE)
    if cfg.promo_enabled:
        check_promos(state, now, dry_run, paused, send, fetch_promos)
    if not scan:
        store.save(STATE_FILE, state)
        return None
    calendar = store.load(CALENDAR_FILE, {})
    history = store.load(HISTORY_FILE, {})
    _sync_cooldown(main_state, state)  # one Google cool-down for both scans
    max_requests = max_requests or int(os.environ.get("LCC_MAX_REQUESTS", "30"))

    old_cfg = getattr(source, "cfg", None)
    if old_cfg is not None:
        source.cfg = lcfg  # GoogleFlights builds its airline filter from source.cfg
    try:
        res = scanner.run(lcfg, source, state, calendar, history, now, max_requests=max_requests)
    finally:
        if old_cfg is not None:
            source.cfg = old_cfg
    res.deals = classify(res.deals, cfg)
    if res.rate_limited:
        _sync_cooldown(state, main_state)
    hits = [d for d in res.deals if d["category"] == "instant"]
    print(f"[廉航] 完成 {res.tasks_done} 個日曆查詢、確認 {res.verified} 組航班；送出 {res.requests} 個請求"
          f"{'、被限流' if res.rate_limited else ''}；資料完整度 {round(res.coverage * 100)}%，"
          f"含行李 < {cfg.lcc.max_total:,} 的有 {len(hits)} 組")
    store.save(CALENDAR_FILE, calendar)
    store.save(HISTORY_FILE, history)

    if not paused and send and pick:
        before = json.loads(json.dumps(state["notified"]))
        hot = pick(res.deals, state, lcfg, now)
        if hot:
            subject, text, html = emails.lcc_instant(hot, cfg)
            if not send(state, subject, text, html, dry_run):
                state["notified"] = before
    state["last_run"] = now.isoformat(timespec="minutes")
    store.save(STATE_FILE, state)
    store.save(LATEST_FILE, {
        "updated": now.isoformat(timespec="minutes"),
        "run": {"ok": res.ok, "requests": res.requests, "failures": res.failures,
                "rate_limited": res.rate_limited, "verified": res.verified, "coverage": res.coverage},
        "config": {"max_total": cfg.lcc.max_total, "bag_kg": cfg.lcc.bag_kg, "airlines": cfg.lcc.airlines,
                   "bag_fees": {a: cfg.lcc.bag_fee(a) for a in cfg.lcc.airlines}},
        "routes": res.routes,
        "deals": res.deals[:200],
    })
    return res
