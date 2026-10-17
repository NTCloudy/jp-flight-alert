"""Incremental, rate-limit-aware scanning.

Google Flights throttles clients that send many requests in a burst (HTTP 429), so the work is
split into small tasks: one calendar request = (route, trip length, ~61-day departure window).
Each task is refreshed on its own schedule — the preferred trip length and the nearest dates most
often (every `interval_hours`), the rest less often — and every run processes whatever is due
within a request budget, stopping immediately on HTTP 429. Results accumulate in calendar.json,
then the most promising date pairs are verified with real itineraries (exact time windows,
airlines, nonstop). Deals and the daily digest always use the freshest data available.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import rules, store
from .airports import airport_name
from .config import Config
from .gflights import CHUNK_DAYS, Itinerary, Leg, RateLimited
from .links import google_flights_url

FRESH_HOURS = 30              # calendar prices older than this are ignored for deals
VERIFY_CACHE_HOURS = 12       # reuse a verification while the calendar price is unchanged
REFERENCE_CACHE_HOURS = 24    # each route's cheapest valid itinerary is refreshed about daily
REFERENCE_DEPTH = 15          # how far down a route's price list to look for that itinerary
MAX_DEALS_PER_ROUTE = 8
MAX_QUEUE_PER_ROUTE = 6       # new itinerary look-ups queued per route per run
NO_SERVICE_RECHECK_DAYS = 7   # routes with no matching flight at all are re-checked weekly
CALENDAR_SHARE = 0.65         # share of the request budget reserved for calendar tasks first
COOLDOWN_MINUTES = 45         # pause after HTTP 429 (doubles on repeats, max 4 h)
MISSING = 1e6                 # "age" of data never fetched


@dataclass
class Task:
    route: str
    nights: int
    chunk: int
    start: date
    end: date
    interval_h: float
    age_h: float
    prefer_gap: int

    @property
    def priority(self) -> tuple:
        return (-(self.age_h / self.interval_h), self.prefer_gap, self.chunk)


@dataclass
class RunResult:
    started: datetime
    routes: dict = field(default_factory=dict)
    deals: list = field(default_factory=list)
    requests: int = 0
    failures: int = 0
    errors: list = field(default_factory=list)
    rate_limited: bool = False
    skipped_cooldown: bool = False
    tasks_done: int = 0
    tasks_due: int = 0
    verified: int = 0
    coverage: float = 0.0

    @property
    def ok(self) -> bool:
        """Healthy: not throttled and most requests succeeded (or nothing needed doing)."""
        if self.rate_limited:
            return False
        return self.requests == 0 or self.failures < self.requests * 0.5


def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _hours_since(iso: str | None, now: datetime) -> float:
    t = _parse(iso)
    return (now - t).total_seconds() / 3600 if t else MISSING


def task_interval(cfg: Config, nights: int, chunk: int) -> float:
    gap = abs(nights + 1 - cfg.prefer_days)
    m_nights = 1 if gap == 0 else 2 if gap == 1 else 4
    m_chunk = (1, 2, 4)[min(chunk, 2)]
    return min(24.0, cfg.interval_hours * m_nights * m_chunk)


def windows(cfg: Config, today: date) -> list[tuple[int, date, date]]:
    start, end = cfg.date_range(today)
    out, k, cur = [], 0, start
    while cur <= end:
        stop = min(cur + timedelta(days=CHUNK_DAYS - 1), end)
        out.append((k, cur, stop))
        cur, k = stop + timedelta(days=1), k + 1
    return out


def _days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _no_service(state: dict, route: str, today: date) -> bool:
    until = (state["routes"].get(route) or {}).get("no_service_until")
    return bool(until and until >= today.isoformat())


def plan(cfg: Config, calendar: dict, state: dict, now: datetime) -> list[Task]:
    """Every due calendar task, most overdue first (never-fetched data first of all)."""
    today = now.date()
    tasks = []
    for origin, dest in cfg.routes():
        route = f"{origin}-{dest}"
        if _no_service(state, route, today):
            continue
        for nights in cfg.durations:
            cache = (calendar.get(route) or {}).get(str(nights)) or {}
            for k, s, e in windows(cfg, today):
                ages = [_hours_since((cache.get(d.isoformat()) or [None, None])[1], now) for d in _days(s, e)]
                age = max(ages) if ages else MISSING
                interval = task_interval(cfg, nights, k)
                if age >= interval * 0.9:
                    tasks.append(Task(route, nights, k, s, e, interval, age, abs(nights + 1 - cfg.prefer_days)))
    tasks.sort(key=lambda t: t.priority)
    return tasks


def _store_calendar(calendar: dict, task: Task, prices: dict, now: datetime) -> None:
    cache = calendar.setdefault(task.route, {}).setdefault(str(task.nights), {})
    stamp = now.isoformat(timespec="minutes")
    for d in _days(task.start, task.end):
        cache[d.isoformat()] = [None, stamp]
    for (dep, ret), total in prices.items():
        if task.start <= dep <= task.end and (ret - dep).days == task.nights:
            cache[dep.isoformat()] = [round(total), stamp]


def _check_no_service(cfg: Config, calendar: dict, state: dict, route: str, now: datetime) -> bool:
    """Mark a route as having no matching service when every preferred-length window is empty."""
    nights = cfg.prefer_days - 1
    if nights not in cfg.durations:
        nights = cfg.durations[0]
    cache = (calendar.get(route) or {}).get(str(nights)) or {}
    for _, s, e in windows(cfg, now.date()):
        for d in _days(s, e):
            entry = cache.get(d.isoformat())
            if not entry or _hours_since(entry[1], now) > 24 * NO_SERVICE_RECHECK_DAYS or entry[0] is not None:
                return False
    state["routes"][route] = {"no_service_until": (now.date() + timedelta(days=NO_SERVICE_RECHECK_DAYS)).isoformat()}
    return True


def prune_calendar(cfg: Config, calendar: dict, today: date) -> None:
    start, end = cfg.date_range(today)
    wanted = {f"{o}-{d}" for o, d in cfg.routes()}
    durations = {str(n) for n in cfg.durations}
    for route in list(calendar):
        if route not in wanted:
            del calendar[route]
            continue
        for n in list(calendar[route]):
            if n not in durations:
                del calendar[route][n]
                continue
            cache = calendar[route][n]
            for d in [d for d in cache if not start.isoformat() <= d <= end.isoformat()]:
                del cache[d]


def fresh_prices(cfg: Config, calendar: dict, route: str, now: datetime) -> dict[tuple[date, date], float]:
    out = {}
    for n, cache in (calendar.get(route) or {}).items():
        for d, (price, at) in cache.items():
            if price is not None and _hours_since(at, now) <= FRESH_HOURS:
                dep = date.fromisoformat(d)
                out[(dep, dep + timedelta(days=int(n)))] = price
    return out


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


def _legs_info(legs: list[Leg]) -> dict:
    first, last = legs[0], legs[-1]
    return {
        "flight": " / ".join(leg.flight for leg in legs),
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


def coverage(cfg: Config, calendar: dict, state: dict, now: datetime) -> float:
    """Share of (route, trip length, departure date) cells with data fresher than FRESH_HOURS."""
    today = now.date()
    start, end = cfg.date_range(today)
    total = fresh = 0
    for origin, dest in cfg.routes():
        route = f"{origin}-{dest}"
        if _no_service(state, route, today):
            continue
        for n in cfg.durations:
            cache = (calendar.get(route) or {}).get(str(n)) or {}
            for d in _days(start, end):
                total += 1
                entry = cache.get(d.isoformat())
                fresh += bool(entry and _hours_since(entry[1], now) <= FRESH_HOURS)
    return fresh / total if total else 1.0


def run(cfg: Config, source, state: dict, calendar: dict, history: dict, now: datetime,
        max_requests: int = 45, max_seconds: float = 15 * 60, clock=time.monotonic) -> RunResult:
    today = now.date()
    res = RunResult(started=now)
    t0 = clock()
    prune_calendar(cfg, calendar, today)
    cooldown = _parse(state.get("cooldown_until"))
    network = not (cooldown and cooldown > now)
    res.skipped_cooldown = not network

    def budget_left(limit: int) -> bool:
        return source.stats.requests < limit and clock() - t0 < max_seconds

    def throttled() -> None:
        nonlocal network
        network = False
        res.rate_limited = True
        streak = int(state.get("rate_limit_streak") or 0) + 1
        state["rate_limit_streak"] = streak
        minutes = min(240, COOLDOWN_MINUTES * 2 ** (streak - 1))
        state["cooldown_until"] = (now + timedelta(minutes=minutes)).isoformat(timespec="minutes")

    # ---- phase 1: calendar tasks ------------------------------------------------------
    def calendar_phase(limit: int) -> None:
        for task in plan(cfg, calendar, state, now):
            if not network or not budget_left(limit):
                return
            if _no_service(state, task.route, today):
                continue
            origin, dest = task.route.split("-")
            try:
                prices = source.calendar(origin, dest, task.nights, task.start, task.end)
            except RateLimited:
                throttled()
                return
            if prices is None:
                continue
            _store_calendar(calendar, task, prices, now)
            res.tasks_done += 1
            _check_no_service(cfg, calendar, state, task.route, now)

    if network:
        calendar_phase(int(max_requests * CALENDAR_SHARE))

    # ---- phase 2: route summaries + verification of promising dates ---------------------
    cache: dict = state["verify_cache"]
    for key in [k for k, v in cache.items() if _hours_since(v.get("at"), now) > 48 or k.split("|")[1] < today.isoformat()]:
        del cache[key]

    priced: dict[str, dict] = {}
    for origin, dest in cfg.routes():
        route = f"{origin}-{dest}"
        info: dict = {"origin": origin, "dest": dest, "name": airport_name(dest)}
        res.routes[route] = info
        if _no_service(state, route, today):
            info.update(no_service=True, recheck=state["routes"][route]["no_service_until"])
            continue
        cal = fresh_prices(cfg, calendar, route, now)
        stamps = [at for n in (calendar.get(route) or {}).values() for _, at in n.values() if at]
        info["updated"] = max(stamps) if stamps else None
        if cal:
            info.update(_summarize(cal, cfg.adults))
            store.record_day(history, route, today, [t / cfg.adults for t in cal.values()])
        avg = store.route_average(history, route, today)
        info["avg30_pp"] = round(avg) if avg else None
        priced[route] = cal

    found_all: list[dict] = []
    queue: list[tuple[float, str, date, date, float, float]] = []  # (pp, route, dep, ret, total, max_age)
    for route, cal in priced.items():
        if not cal:
            continue
        avg = res.routes[route].get("avg30_pp")
        ceiling = max(cfg.digest_max, rules.anomaly_ceiling(cfg, avg))
        ranked = sorted(cal.items(), key=lambda kv: (kv[1], abs((kv[0][1] - kv[0][0]).days + 1 - cfg.prefer_days)))
        found: dict[str, dict] = {}
        need_reference = True
        queued = 0
        for i, ((dep, ret), total) in enumerate(ranked):
            cheap = total / cfg.adults <= ceiling
            if not cheap and (i >= REFERENCE_DEPTH or not need_reference):
                break
            key = f"{route}|{dep}|{ret}"
            entry = cache.get(key)
            max_age = VERIFY_CACHE_HOURS if cheap else REFERENCE_CACHE_HOURS
            if entry and entry.get("cal_total") == total and _hours_since(entry.get("at"), now) < max_age:
                if entry.get("deal") and len(found) < MAX_DEALS_PER_ROUTE:
                    found[key] = entry["deal"]
                    need_reference = False
            elif queued < MAX_QUEUE_PER_ROUTE:
                queue.append((total / cfg.adults, route, dep, ret, total, max_age))
                queued += 1
                if not cheap:
                    need_reference = False  # one reference look-up per route per run is enough
        found_all.extend(found.values())

    # verify cheapest first (the instant-alert range first of all)
    queue.sort(key=lambda q: (q[0] > cfg.instant_max, q[0] > cfg.digest_max, q[0]))
    for pp, route, dep, ret, total, _ in queue:
        if not network or not budget_left(max_requests):
            break
        origin, dest = route.split("-")
        try:
            its = source.itineraries(origin, dest, dep, ret)
        except RateLimited:
            throttled()
            break
        if its is None:
            continue
        res.verified += 1
        picked = rules.best(its, cfg)
        deal = make_deal(cfg, origin, dest, dep, ret, picked[0], picked[1], now) if picked else None
        cache[f"{route}|{dep}|{ret}"] = {"cal_total": total, "at": now.isoformat(timespec="minutes"), "deal": deal}
        if deal:
            found_all.append(deal)

    # ---- phase 3: leftover budget goes back to calendar tasks ----------------------------
    if network:
        calendar_phase(max_requests)

    # ---- classify deals (thresholds may have changed since verification) ---------------
    seen: set[str] = set()
    for deal in sorted(found_all, key=lambda d: d["verified_at"], reverse=True):
        if deal["key"] in seen or deal["dep"] < cfg.date_range(today)[0].isoformat():
            continue
        seen.add(deal["key"])
        verdict = rules.classify(deal["price_pp"], cfg, res.routes.get(deal["route"], {}).get("avg30_pp"))
        res.deals.append({**deal, "category": verdict.category, "discount_pct": verdict.discount_pct})
    res.deals.sort(key=lambda d: (d["price_pp"], abs(d["days"] - cfg.prefer_days), d["dep"]))

    res.requests, res.failures = source.stats.requests, source.stats.failures
    res.errors = list(source.stats.errors)
    if network and not res.rate_limited and res.requests:
        state["rate_limit_streak"] = 0
    res.tasks_due = len(plan(cfg, calendar, state, now))
    res.coverage = round(coverage(cfg, calendar, state, now), 3)
    return res
