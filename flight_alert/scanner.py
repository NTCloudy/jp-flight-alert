"""Scan pipeline: calendar prices -> candidate dates -> verified itineraries -> classified deals."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import rules, store
from .airports import airport_name
from .config import Config
from .gflights import Itinerary, Leg
from .links import google_flights_url

VERIFY_PER_ROUTE = 4          # new itinerary look-ups per route per scan for qualifying dates
MAX_DEALS_PER_ROUTE = 8       # verified deals kept per route per scan
VERIFY_CACHE_HOURS = 12       # reuse a verification while the calendar price is unchanged
REFERENCE_CACHE_HOURS = 24    # each route's overall cheapest valid itinerary is refreshed about daily
REFERENCE_FRESH_PER_SCAN = 2  # new look-ups per route per scan for that reference itinerary
REFERENCE_DEPTH = 15          # how far down the price list to look for a valid reference itinerary
NO_SERVICE_RECHECK_DAYS = 7   # routes without any matching flight are re-checked weekly
MAX_SCAN_SECONDS = 40 * 60


@dataclass
class ScanResult:
    started: datetime
    finished: datetime | None = None
    routes: dict = field(default_factory=dict)
    deals: list = field(default_factory=list)
    requests: int = 0
    failures: int = 0
    errors: list = field(default_factory=list)
    candidates: int = 0
    verified: int = 0

    @property
    def ok(self) -> bool:
        return self.requests > 0 and self.failures < self.requests * 0.5


def _hours_since(iso: str | None, now: datetime) -> float:
    if not iso:
        return 1e9
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return 1e9


def _legs_info(legs: list[Leg]) -> dict:
    first, last = legs[0], legs[-1]
    return {
        "flight": " / ".join(l.flight for l in legs),
        "airline": first.airline,
        "from": first.origin,
        "to": last.dest,
        "dep": f"{first.dep:%H:%M}",
        "arr": f"{last.arr:%H:%M}",
    }


def make_deal(cfg: Config, origin: str, dest: str, dep: date, ret: date, it: Itinerary,
              flags: list[str], now: datetime) -> dict:
    return {
        "key": f"{origin}-{dest}|{dep}|{ret}",
        "route": f"{origin}-{dest}",
        "origin": origin,
        "dest": dest,
        "dest_name": airport_name(dest),
        "dep": dep.isoformat(),
        "ret": ret.isoformat(),
        "days": (ret - dep).days + 1,
        "price_pp": round(it.total / cfg.adults),
        "price_total": round(it.total),
        "adults": cfg.adults,
        "out": _legs_info(it.out),
        "back": _legs_info(it.ret),
        "flags": flags,
        "verified_at": now.isoformat(timespec="minutes"),
        "url": google_flights_url(origin, dest, dep, ret, cfg.adults, cfg.airlines, cfg.nonstop_only),
    }


def _summarize(cal: dict[tuple[date, date], float], adults: int) -> dict:
    pps = {k: v / adults for k, v in cal.items()}
    (dep, ret), best = min(pps.items(), key=lambda kv: kv[1])
    by_days: dict[str, int] = {}
    by_month: dict[str, int] = {}
    for (d, r), pp in pps.items():
        days = str((r - d).days + 1)
        by_days[days] = min(by_days.get(days, 10**9), round(pp))
        month = d.strftime("%Y-%m")
        by_month[month] = min(by_month.get(month, 10**9), round(pp))
    return {
        "min_pp": round(best),
        "min_dep": dep.isoformat(),
        "min_ret": ret.isoformat(),
        "med_pp": round(statistics.median(pps.values())),
        "dates": len(pps),
        "by_days": dict(sorted(by_days.items(), key=lambda kv: int(kv[0]))),
        "by_month": dict(sorted(by_month.items())),
    }


def scan(cfg: Config, source, state: dict, history: dict, now: datetime, clock=time.monotonic) -> ScanResult:
    today = now.date()
    res = ScanResult(started=now)
    t0 = clock()
    start, end = cfg.date_range(today)
    calendars: dict[str, dict] = {}

    # ---- stage 1: calendar prices for every route and trip length --------------------
    for origin, dest in cfg.routes():
        rkey = f"{origin}-{dest}"
        info: dict = {"origin": origin, "dest": dest, "name": airport_name(dest)}
        res.routes[rkey] = info
        until = (state["routes"].get(rkey) or {}).get("no_service_until")
        if until and until >= today.isoformat():
            info.update(no_service=True, skipped=True, recheck=until)
            continue
        if clock() - t0 > MAX_SCAN_SECONDS:
            info["failed"] = True
            res.errors.append(f"{rkey}: 超過單次檢查時間上限，下次再查")
            continue
        cal: dict[tuple[date, date], float] = {}
        failed = 0
        for nights in cfg.durations:
            prices = source.calendar(origin, dest, nights, start, end)
            if prices is None:
                failed += 1
                continue
            for (d, r), total in prices.items():
                if start <= d <= end and (r - d).days == nights:
                    cal[(d, r)] = total
        if failed == len(cfg.durations):
            info["failed"] = True
        elif not cal and failed == 0:
            state["routes"][rkey] = {"no_service_until": (today + timedelta(days=NO_SERVICE_RECHECK_DAYS)).isoformat()}
            info["no_service"] = True
        elif cal:
            state["routes"].pop(rkey, None)
            info.update(_summarize(cal, cfg.adults))
            store.record_day(history, rkey, today, [t / cfg.adults for t in cal.values()])
        avg = store.route_average(history, rkey, today)
        info["avg30_pp"] = round(avg) if avg else None
        calendars[rkey] = cal

    # ---- stage 2: verify the most promising date pairs with real itineraries -----------
    cache: dict = state["verify_cache"]
    for key in [k for k, v in cache.items()
                if _hours_since(v.get("at"), now) > 48 or k.split("|")[1] < today.isoformat()]:
        del cache[key]

    for rkey, cal in calendars.items():
        if not cal:
            continue
        origin, dest = rkey.split("-")
        avg = res.routes[rkey].get("avg30_pp")
        ceiling = max(cfg.digest_max, rules.anomaly_ceiling(cfg, avg))
        ranked = sorted(cal.items(), key=lambda kv: (kv[1], abs((kv[0][1] - kv[0][0]).days + 1 - cfg.prefer_days)))
        found: dict[str, dict] = {}

        def keep(key: str, deal: dict | None) -> bool:
            if deal and key not in found:
                verdict = rules.classify(deal["price_pp"], cfg, avg)
                found[key] = {**deal, "category": verdict.category, "discount_pct": verdict.discount_pct}
            return bool(deal)

        def cached(key: str, total: float, max_age: float):
            entry = cache.get(key)
            if entry and entry.get("cal_total") == total and _hours_since(entry.get("at"), now) < max_age:
                return True, entry.get("deal")
            return False, None

        def verify(key: str, dep: date, ret: date, total: float) -> dict | None:
            its = source.itineraries(origin, dest, dep, ret)
            if its is None:  # request failed; try again next scan
                return None
            res.verified += 1
            picked = rules.best(its, cfg)
            deal = make_deal(cfg, origin, dest, dep, ret, picked[0], picked[1], now) if picked else None
            cache[key] = {"cal_total": total, "at": now.isoformat(timespec="minutes"), "deal": deal}
            return deal

        def out_of_time() -> bool:
            return clock() - t0 > MAX_SCAN_SECONDS

        # (a) every date pair cheap enough to matter, cheapest first. Pairs already known to have no
        #     itinerary inside the exact time windows are skipped without spending a request.
        budget = VERIFY_PER_ROUTE
        for (dep, ret), total in ranked:
            if total / cfg.adults > ceiling or len(found) >= MAX_DEALS_PER_ROUTE:
                break
            key = f"{rkey}|{dep}|{ret}"
            hit, deal = cached(key, total, VERIFY_CACHE_HOURS)
            if hit:
                keep(key, deal)
                continue
            if budget <= 0 or out_of_time():
                continue
            budget -= 1
            res.candidates += 1
            keep(key, verify(key, dep, ret, total))

        # (b) the route's cheapest valid itinerary overall (for the destination price table).
        budget = REFERENCE_FRESH_PER_SCAN
        for (dep, ret), total in ranked[:REFERENCE_DEPTH]:
            key = f"{rkey}|{dep}|{ret}"
            hit, deal = cached(key, total, REFERENCE_CACHE_HOURS)
            if not hit:
                if budget <= 0 or out_of_time():
                    continue
                budget -= 1
                deal = verify(key, dep, ret, total)
            if keep(key, deal):
                break
        res.deals.extend(found.values())

    res.deals.sort(key=lambda d: (d["price_pp"], abs(d["days"] - cfg.prefer_days), d["dep"]))
    stats = source.stats
    res.requests, res.failures = stats.requests, stats.failures
    res.errors.extend(stats.errors)
    return res
