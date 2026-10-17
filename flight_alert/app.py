"""Orchestration: one run = incremental scan + promo check + instant alerts + daily digest."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from . import emails, promo, scanner, store
from .config import TPE_TZ, Config, now_tpe
from .notifier import send_email

RENOTIFY_AFTER_GONE_HOURS = 12
HEALTH_ALERT_AFTER_FAILED_RUNS = 6


def _hours_since(iso: str | None, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 3600 if iso else 1e9
    except ValueError:
        return 1e9


def pick_instant(deals: list[dict], state: dict, cfg: Config, now: datetime) -> list[dict]:
    """Deals in the instant range that are new, came back, or got cheaper by `renotify_drop`."""
    notified: dict = state["notified"]
    stamp = now.isoformat(timespec="minutes")
    muted = set(cfg.muted)
    current = {d["key"]: d for d in deals if d.get("category") == "instant" and d["key"] not in muted}
    send = []
    for key, d in current.items():
        prev = notified.get(key)
        back = prev and prev.get("gone_since") and _hours_since(prev["gone_since"], now) >= RENOTIFY_AFTER_GONE_HOURS
        if prev is None or back or d["price_pp"] <= prev["price_pp"] - cfg.renotify_drop:
            send.append(d)
            notified[key] = {"price_pp": d["price_pp"], "at": stamp, "gone_since": None}
        else:
            prev["gone_since"] = None  # compare future drops with the last *notified* price
    today = now.date().isoformat()
    for key in list(notified):
        if key in current:
            continue
        prev = notified[key]
        if key.split("|")[1] < today or _hours_since(prev.get("gone_since"), now) > 72 and prev.get("gone_since"):
            del notified[key]
        elif not prev.get("gone_since"):
            prev["gone_since"] = stamp
    send.sort(key=lambda d: d["price_pp"])
    return send


def digest_due(cfg: Config, state: dict, now: datetime) -> bool:
    local = now.astimezone(TPE_TZ)
    return local.hour >= cfg.digest_hour and state.get("digest_last_date") != local.date().isoformat()


def _stats(state: dict, now: datetime) -> dict:
    recent = [s for s in state.get("scan_log", []) if _hours_since(s.get("at"), now) <= 24]
    req = sum(s.get("requests", 0) for s in recent)
    fail = sum(s.get("failures", 0) for s in recent)
    return {
        "runs_24h": len(recent),
        "requests_24h": req,
        "success_rate_24h": f"{round((req - fail) / req * 100)}%" if req else "—",
        "rate_limited_24h": sum(1 for s in recent if s.get("rate_limited")),
    }


def build_latest(cfg: Config, state: dict, res: scanner.RunResult, now: datetime) -> dict:
    return {
        "updated": now.isoformat(timespec="minutes"),
        "run": {
            "ok": res.ok, "requests": res.requests, "failures": res.failures, "rate_limited": res.rate_limited,
            "skipped_cooldown": res.skipped_cooldown, "tasks_done": res.tasks_done, "tasks_due": res.tasks_due,
            "verified": res.verified, "suspect_empty": res.suspect_empty,
        },
        "errors": res.errors[:10],
        "coverage": res.coverage,
        "cooldown_until": state.get("cooldown_until"),
        "stats": _stats(state, now),
        "routes": res.routes,
        "deals": res.deals[:300],
        "promos": (state.get("promo_recent") or [])[:20],
        "notified": sorted(state["notified"]),
        "digest_last_date": state.get("digest_last_date"),
        "config": {"instant_max": cfg.instant_max, "digest_max": cfg.digest_max, "anomaly_pct": cfg.anomaly_pct,
                   "adults": cfg.adults, "interval_hours": cfg.interval_hours, "digest_hour": cfg.digest_hour},
    }


def run(cfg: Config, mode: str = "auto", dry_run: bool = False, source=None, now: datetime | None = None,
        max_requests: int | None = None) -> int:
    now = now or now_tpe()
    state = store.load_state()
    calendar = store.load("calendar.json", {})
    history = store.load("history.json", {})
    paused = cfg.is_paused(now.date())
    max_requests = max_requests or int(os.environ.get("MAX_REQUESTS", "60"))
    if not state.get("last_run") and not state.get("digest_last_date") and mode != "digest":
        # Brand-new install: the first daily digest goes out the next morning, once enough
        # prices have been collected (instant alerts work from the first run).
        local = now.astimezone(TPE_TZ)
        if local.hour >= cfg.digest_hour:
            state["digest_last_date"] = local.date().isoformat()

    if mode in ("auto", "scan"):
        if source is None:
            from .gflights import GoogleFlights

            source = GoogleFlights(cfg)
        res = scanner.run(cfg, source, state, calendar, history, now, max_requests=max_requests, force=(mode == "scan"))
        print(f"[掃描] 完成 {res.tasks_done} 個日曆查詢、確認 {res.verified} 組航班；送出 {res.requests} 個請求、失敗 {res.failures} 次"
              f"{f'、可疑空白 {res.suspect_empty} 次' if res.suspect_empty else ''}"
              f"{'、被限流' if res.rate_limited else ''}{'、冷卻中略過' if res.skipped_cooldown else ''}；"
              f"資料完整度 {round(res.coverage * 100)}%，待查 {res.tasks_due} 項，符合條件 {len(res.deals)} 組")
        for e in res.errors[:5]:
            print("   [錯誤]", e)

        # promotions (cheap: one RSS request)
        if cfg.promo_enabled and _hours_since(state.get("promo_checked_at"), now) >= 0.9:
            try:
                new = promo.select(promo.fetch(), state["promo_seen"], now, recent=state.get("promo_recent"))
                promo.update_state(state, new, now)
                state["promo_checked_at"] = now.isoformat(timespec="minutes")
                print(f"[促銷] 新消息 {len(new)} 則")
            except Exception as e:  # noqa: BLE001
                print(f"[促銷] 讀取失敗：{type(e).__name__}: {e}")

        # instant alerts
        if not paused:
            hot = pick_instant(res.deals, state, cfg, now)
            if hot:
                subject, text, html = emails.instant(hot, cfg)
                send_email(subject, text, html, dry_run=dry_run)

        # health tracking
        if not res.skipped_cooldown:
            if res.ok:
                if state.get("health_alert_at") and not paused:
                    send_email("✅ 日本機票提醒：查詢已恢復正常", "Google Flights 查詢恢復正常，會繼續幫你盯票。", dry_run=dry_run)
                state["fail_streak"], state["health_alert_at"] = 0, None
            else:
                state["fail_streak"] = int(state.get("fail_streak") or 0) + 1
                if (state["fail_streak"] >= HEALTH_ALERT_AFTER_FAILED_RUNS and not paused
                        and _hours_since(state.get("health_alert_at"), now) >= 24):
                    problem = "Google Flights 查詢一直被限流" if res.rate_limited else "Google Flights 查詢一直失敗"
                    detail = f"連續 {state['fail_streak']} 次執行沒有成功。最近的錯誤：\n" + "\n".join(res.errors[:5])
                    subject, text = emails.health(problem, detail)
                    send_email(subject, text, dry_run=dry_run)
                    state["health_alert_at"] = now.isoformat(timespec="minutes")
            state["scan_log"] = (state.get("scan_log") or [])[-47:] + [{
                "at": now.isoformat(timespec="minutes"), "ok": res.ok, "requests": res.requests,
                "failures": res.failures, "rate_limited": res.rate_limited, "tasks_done": res.tasks_done,
                "verified": res.verified, "deals": len(res.deals)}]
        state["last_run"] = now.isoformat(timespec="minutes")
        latest = build_latest(cfg, state, res, now)
        store.save("calendar.json", calendar)
        store.save("history.json", history)
        store.save("latest.json", latest)
    else:
        latest = store.load("latest.json", {})

    if mode == "digest" or (mode in ("auto", "scan") and digest_due(cfg, state, now)):
        if paused and mode != "digest":
            print("[整理] 通知暫停中，略過每日整理")
        else:
            subject, text, html = emails.digest(cfg, latest, state.get("promo_pending") or [], now)
            send_email(subject, text, html, dry_run=dry_run)
            state["digest_last_date"] = now.astimezone(TPE_TZ).date().isoformat()
            state["promo_pending"] = []
            if latest:
                latest["digest_last_date"] = state["digest_last_date"]
                store.save("latest.json", latest)
    store.save("state.json", state)
    return 0


def test_email(dry_run: bool = False) -> int:
    subject = "✅ 日本機票提醒：測試信"
    text = "收到這封信代表 Email 通知設定成功！\n\n網頁控制台：" + emails.CONSOLE_URL
    html = emails._page("✅ 測試信", emails._section("設定成功", "<div style=\"font-size:14px\">收到這封信代表 Email 通知設定成功！</div>"))
    send_email(subject, text, html, dry_run=dry_run)
    return 0


def next_digest_time(cfg: Config, state: dict, now: datetime) -> datetime:
    local = now.astimezone(TPE_TZ)
    target = local.replace(hour=cfg.digest_hour, minute=0, second=0, microsecond=0)
    if state.get("digest_last_date") == local.date().isoformat() or local >= target + timedelta(hours=1):
        target += timedelta(days=1)
    return target
