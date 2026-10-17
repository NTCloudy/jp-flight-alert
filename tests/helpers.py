"""Shared test helpers: a fake Google Flights source and itinerary builders."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from flight_alert.config import TPE_TZ, Config
from flight_alert.gflights import Itinerary, Leg, RateLimited, SourceStats

NOW = datetime(2026, 10, 17, 21, 0, tzinfo=TPE_TZ)


def leg(code: str, num: str, frm: str, to: str, day: date, dep: str, arr: str, operating: str | None = None) -> Leg:
    h1, m1 = map(int, dep.split(":"))
    h2, m2 = map(int, arr.split(":"))
    d = datetime(day.year, day.month, day.day)
    return Leg(airline=code, flight=f"{code}{num}", origin=frm, dest=to, dep=d.replace(hour=h1, minute=m1),
               arr=d.replace(hour=h2, minute=m2), operating=operating)


def itin(dep: date, ret: date, total: float, out=("JX", "870", "11:40", "14:15"), back=("JX", "871", "14:25", "15:10"),
         origin="TPE", dest="OKA") -> Itinerary:
    return Itinerary(
        out=[leg(out[0], out[1], origin, dest, dep, out[2], out[3])],
        ret=[leg(back[0], back[1], dest, origin, ret, back[2], back[3])],
        total=total,
    )


class FakeSource:
    """calendar_prices: {route: {(dep, ret): total}}; itins: {(route, dep, ret): [Itinerary]}"""

    def __init__(self, calendar_prices=None, itins=None, rate_limit_after: int | None = None, fail_routes=()):
        self.calendar_prices = calendar_prices or {}
        self.itins = itins or {}
        self.rate_limit_after = rate_limit_after
        self.fail_routes = set(fail_routes)
        self.stats = SourceStats()
        self.calendar_calls: list[tuple] = []
        self.itinerary_calls: list[tuple] = []

    def _tick(self):
        self.stats.requests += 1
        if self.rate_limit_after is not None and self.stats.requests > self.rate_limit_after:
            self.stats.rate_limited = True
            raise RateLimited("429")

    def calendar(self, origin, dest, nights, start, end):
        self._tick()
        route = f"{origin}-{dest}"
        self.calendar_calls.append((route, nights, start, end))
        if route in self.fail_routes:
            self.stats.fail(f"{route} failed")
            return None
        return {(d, r): p for (d, r), p in self.calendar_prices.get(route, {}).items()
                if start <= d <= end and (r - d).days == nights}

    def itineraries(self, origin, dest, dep, ret):
        self._tick()
        route = f"{origin}-{dest}"
        self.itinerary_calls.append((route, dep, ret))
        return list(self.itins.get((route, dep, ret), []))


def small_config(**over) -> Config:
    base = dict(origins=["TPE"], destinations=["OKA", "NRT"], min_days=5, max_days=5, horizon_days=60, lead_days=2)
    base.update(over)
    return Config(**base)


def day(offset: int) -> date:
    return NOW.date() + timedelta(days=offset)
