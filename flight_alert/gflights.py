"""Google Flights access through the open-source `fli` library (package name: flights).

`fli` talks to Google Flights' internal endpoints (GetCalendarGraph / GetShoppingResults).
This module wraps it with: an Okinawa airport-code fix, one-request-at-a-time pacing,
rate-limit detection (HTTP 429 stops the run instead of hammering), and plain-dict results
that the rest of the app (and the tests) can use without fli.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import Config, Window

log = logging.getLogger(__name__)
logging.getLogger("fli").setLevel(logging.ERROR)

CHUNK_DAYS = 61  # Google returns at most ~61 departure dates per calendar request
DEFAULT_GAP = float(os.environ.get("FLIGHT_ALERT_GAP", "5"))  # seconds between requests


class RateLimited(RuntimeError):
    """Google answered HTTP 429: stop querying for a while."""


class EmptyResponse(RuntimeError):
    """Google answered 200 but without any result rows (happens now and then); retry later."""


def _patch_okinawa() -> None:
    """fli's Airport enum maps both OKA and Indonesia's NAH to "Naha Airport", so Python makes
    OKA an alias of NAH (requests would go to Indonesia and OKA rows fail to parse).
    Give OKA its own enum member."""
    from fli.models import Airport

    if Airport.__members__["OKA"].name == "OKA":
        return
    obj = object.__new__(Airport)
    obj._name_ = "OKA"
    obj._value_ = "Naha Airport (Okinawa)"
    obj._sort_order_ = len(Airport._member_names_)
    Airport._member_map_["OKA"] = obj
    Airport._value2member_map_[obj._value_] = obj
    Airport._member_names_.append("OKA")
    try:
        type.__setattr__(Airport, "OKA", obj)
    except (AttributeError, TypeError):
        pass
    import fli.search._decoders as decoders

    decoders._AIRPORT_BY_CODE["OKA"] = obj


class Pacer:
    def __init__(self, gap: float):
        self.gap = gap
        self._last = 0.0

    def wait(self) -> None:
        target = self.gap * random.uniform(0.75, 1.5)
        delay = target - (time.monotonic() - self._last)
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()


def _install_paced_client(pacer: Pacer, stats: "SourceStats", timeout: float = 30) -> None:
    """Replace fli's shared HTTP client `post` with a paced, non-retrying, counting version and
    make fli run its internal follow-up requests sequentially (no bursts)."""
    from fli.search import _concurrency
    from fli.search import client as fli_client

    _concurrency._executor_max_workers = 1
    c = fli_client.get_client()

    def post(url: str, **kwargs):
        pacer.wait()
        stats.requests += 1
        kwargs.setdefault("timeout", timeout)
        try:
            r = c._session().post(url, **kwargs)
        except Exception as e:  # noqa: BLE001
            raise fli_client._wrap_request_error("POST", url, e) from e
        if r.status_code == 429:
            raise RateLimited("Google Flights 回應 HTTP 429（查詢太頻繁）")
        if r.status_code >= 400:
            raise RuntimeError(f"Google Flights 回應 HTTP {r.status_code}")
        return r

    c.post = post


@dataclass
class Leg:
    airline: str
    flight: str
    origin: str
    dest: str
    dep: datetime
    arr: datetime
    operating: str | None = None


@dataclass
class Itinerary:
    out: list[Leg]
    ret: list[Leg]
    total: float  # price for all passengers, TWD


@dataclass
class SourceStats:
    requests: int = 0  # HTTP requests actually sent
    failures: int = 0
    empty: int = 0     # answers without any result rows (after one retry)
    rate_limited: bool = False
    errors: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.failures += 1
        if len(self.errors) < 20:
            self.errors.append(msg)


class GoogleFlights:
    """Real data source. Network problems are recorded in `stats`; HTTP 429 raises RateLimited."""

    def __init__(self, cfg: Config, gap: float = DEFAULT_GAP, retries: int = 1):
        _patch_okinawa()
        from fli.models import Airline, Airport
        from fli.search import SearchDates, SearchFlights

        self.stats = SourceStats()
        self.last_note = ""  # why the last itinerary look-up found nothing usable
        _install_paced_client(Pacer(gap), self.stats)
        self._Airline, self._Airport = Airline, Airport
        self._dates, self._flights = SearchDates(), SearchFlights()
        self.cfg = cfg
        self.retries = retries

    # ---- plumbing --------------------------------------------------------
    def _call(self, what: str, fn):
        """Returns (result, status) with status "ok", "empty" (Google answered without results,
        twice) or "fail". HTTP 429 raises RateLimited."""
        for attempt in range(self.retries + 1):
            try:
                return fn(), "ok"
            except RateLimited:
                self.stats.rate_limited = True
                self.stats.fail(f"{what}: HTTP 429")
                raise
            except EmptyResponse:
                if attempt == self.retries:
                    self.stats.empty += 1
                    return None, "empty"
                time.sleep(3 + random.uniform(0, 3))
            except Exception as e:  # noqa: BLE001 - network/parse errors must not stop the scan
                msg = f"{what}: {type(e).__name__}: {str(e)[:160]}"
                log.warning("%s (attempt %d)", msg, attempt + 1)
                if attempt == self.retries:
                    self.stats.fail(msg)
                    return None, "fail"
                time.sleep(5 + random.uniform(0, 5))
        return None, "fail"

    def _restrictions(self, w: Window):
        from fli.models.google_flights.base import TimeRestrictions

        # Google filters by whole hours and treats the latest hour as inclusive (14 => up to 14:59),
        # so this is a superset; exact minutes are enforced later in rules.py.
        return TimeRestrictions(
            earliest_departure=w.depart_earliest // 60,
            latest_departure=max(1, w.depart_latest // 60),
            latest_arrival=max(1, w.arrive_latest // 60),
        )

    def _segments(self, origin: str, dest: str, dep: date, ret: date):
        from fli.models.google_flights.base import FlightSegment

        A = self._Airport
        return [
            FlightSegment(departure_airport=[[A[origin], 0]], arrival_airport=[[A[dest], 0]],
                          travel_date=dep.isoformat(), time_restrictions=self._restrictions(self.cfg.outbound)),
            FlightSegment(departure_airport=[[A[dest], 0]], arrival_airport=[[A[origin], 0]],
                          travel_date=ret.isoformat(), time_restrictions=self._restrictions(self.cfg.inbound)),
        ]

    def _common(self):
        from fli.models.google_flights.base import MaxStops, PassengerInfo, SeatType, TripType

        return dict(
            trip_type=TripType.ROUND_TRIP,
            passenger_info=PassengerInfo(adults=self.cfg.adults),
            stops=MaxStops.NON_STOP if self.cfg.nonstop_only else MaxStops.ANY,
            seat_type=SeatType.ECONOMY,
            airlines=[self._Airline[c] for c in self.cfg.airlines],
        )

    # ---- public API ------------------------------------------------------
    def calendar(self, origin: str, dest: str, nights: int, start: date, end: date) -> dict[tuple[date, date], float] | None:
        """Cheapest total price per (departure, return) date pair for departures in [start, end]
        (at most CHUNK_DAYS days: one request). Dates without matching flights are simply absent,
        so {} means "nothing found" (Google sometimes says that by mistake; the scanner decides
        whether to trust it). None if the request failed."""
        from fli.models.google_flights.dates import DateSearchFilters
        from fli.search._urls import with_locale_params
        from fli.search._wire import parse_first_wrb_payload

        end = min(end, start + timedelta(days=CHUNK_DAYS - 1))
        filters = DateSearchFilters(
            **self._common(),
            flight_segments=self._segments(origin, dest, start, start + timedelta(days=nights)),
            from_date=start.isoformat(), to_date=end.isoformat(), duration=nights,
        )
        url = with_locale_params(self._dates.BASE_URL, "TWD", "zh-TW", "TW")

        def fetch():
            r = self._dates.client.post(url=url, data=f"f.req={filters.encode()}", impersonate="chrome",
                                        allow_redirects=True)
            data = parse_first_wrb_payload(r.text)
            rows = data[-1] if isinstance(data, list) and data and isinstance(data[-1], list) else None
            if not rows:
                raise EmptyResponse("Google 回傳空白日曆")
            return rows

        rows, status = self._call(f"calendar {origin}-{dest} {nights}晚 {start}", fetch)
        if status == "fail":
            return None
        prices: dict[tuple[date, date], float] = {}
        for row in rows or []:
            try:
                dep, ret = date.fromisoformat(row[0]), date.fromisoformat(row[1])
                price = float(row[2][0][1]) if row[2] else None
            except (TypeError, ValueError, IndexError):
                continue
            if price:
                prices[(dep, ret)] = min(prices.get((dep, ret), float("inf")), price)
        return prices

    def itineraries(self, origin: str, dest: str, dep: date, ret: date,
                    outbound_ok: Callable[[list[Leg]], bool] | None = None,
                    max_outbound: int = 3) -> list[Itinerary] | None:
        """Round-trip combinations for these dates: one request for the outbound list, then one per
        outbound (cheapest `max_outbound` that pass `outbound_ok`) for its return options and the
        round-trip total. [] = nothing bookable right now (or no outbound passes `outbound_ok`);
        None = the request failed."""
        from fli.models.google_flights.base import SortBy
        from fli.models.google_flights.flights import FlightSearchFilters

        filters = FlightSearchFilters(**self._common(), flight_segments=self._segments(origin, dest, dep, ret),
                                      sort_by=SortBy.CHEAPEST)
        locale = dict(currency="TWD", language="zh-TW", country="TW")

        def fetch(f, capture: bool, what: str):
            rows = self._flights._fetch_flights(f, capture_session=capture, **locale)
            rows = [r for r in rows or [] if r.price is not None and r.legs]
            if not rows:
                raise EmptyResponse(f"Google 沒有回傳{what}")
            return rows

        outs, status = self._call(f"flights {origin}-{dest} {dep}~{ret}", lambda: fetch(filters, True, "去程航班"))
        self.last_note = ""
        if status != "ok":
            self.last_note = "Google 目前查不到可訂的航班（可能已售完）"
            return None if status == "fail" else []
        picked = [o for o in outs if outbound_ok is None or outbound_ok([_leg(x) for x in o.legs])]
        if not picked:
            o = min(outs, key=lambda x: x.price)
            first, last = o.legs[0], o.legs[-1]
            self.last_note = (f"去程沒有符合時段的航班（最便宜的 {_leg(first).flight} "
                              f"{first.departure_datetime:%H:%M} 起飛、{last.arrival_datetime:%H:%M} 抵達）")
            return []
        picked = sorted(picked, key=lambda o: o.price)[:max_outbound]
        result: list[Itinerary] = []
        failed = 0
        for o in picked:
            f2 = deepcopy(filters)
            f2.flight_segments[0].selected_flight = o
            rets, status = self._call(f"return {origin}-{dest} {dep}~{ret} {o.legs[0].flight_number}",
                                      lambda f2=f2: fetch(f2, False, "回程航班"))
            if status != "ok":
                failed += status == "fail"
                continue
            out_legs = [_leg(x) for x in o.legs]
            result += [Itinerary(out=out_legs, ret=[_leg(x) for x in r.legs], total=float(r.price)) for r in rets]
        if picked and failed == len(picked):
            return None
        if picked and not result:
            self.last_note = "回程目前查不到可訂的航班（可能已售完）"
        return result


def _code(enum_member) -> str:
    return enum_member.name.removeprefix("_") if enum_member is not None else ""


def _leg(leg) -> Leg:
    return Leg(
        airline=_code(leg.airline),
        flight=f"{_code(leg.airline)}{leg.flight_number}",
        origin=_code(leg.departure_airport),
        dest=_code(leg.arrival_airport),
        dep=leg.departure_datetime,
        arr=leg.arrival_datetime,
        operating=_code(leg.operating_airline) if leg.operating_airline else None,
    )
