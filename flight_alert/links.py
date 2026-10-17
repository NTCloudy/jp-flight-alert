"""Google Flights deep links (the `tfs` protobuf parameter of google.com/travel/flights/search).

Field numbers follow the community-maintained schema used by fast-flights (flights.proto):
  Info:       3 = FlightData (one per leg), 8 = passengers (packed enum, 1 = adult),
              9 = seat (1 = economy), 19 = trip (1 = round trip)
  FlightData: 2 = date, 5 = max stops (0 = nonstop), 6 = airlines,
              8/9 = earliest/latest departure hour, 10/11 = earliest/latest arrival hour,
              13 = from airport, 14 = to airport
"""

from __future__ import annotations

import base64
from datetime import date
from urllib.parse import urlencode


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(num: int, wire: int, payload: bytes | int) -> bytes:
    tag = _varint((num << 3) | wire)
    if wire == 0:
        return tag + _varint(int(payload))
    return tag + _varint(len(payload)) + payload  # wire == 2


def _str(num: int, s: str) -> bytes:
    return _field(num, 2, s.encode())


def _flight_data(day: date, origin: str, dest: str, airlines: list[str], nonstop: bool,
                 hours: tuple[int | None, int | None, int | None] | None) -> bytes:
    msg = _str(2, day.isoformat())
    if nonstop:
        msg += _field(5, 0, 0)
    for a in airlines:
        msg += _str(6, a)
    if hours:
        earliest_dep, latest_dep, latest_arr = hours
        if earliest_dep is not None:
            msg += _field(8, 0, earliest_dep)
        if latest_dep is not None:
            msg += _field(9, 0, latest_dep)
        if latest_arr is not None:
            msg += _field(11, 0, latest_arr)
    msg += _field(13, 2, _str(2, origin))
    msg += _field(14, 2, _str(2, dest))
    return msg


def google_flights_url(origin: str, dest: str, dep: date, ret: date, adults: int = 2,
                       airlines: list[str] | None = None, nonstop: bool = True,
                       out_hours: tuple[int | None, int | None, int | None] | None = None,
                       ret_hours: tuple[int | None, int | None, int | None] | None = None) -> str:
    """Round-trip economy search with passengers, nonstop, airlines and (whole-hour) time filters
    pre-applied. `*_hours` = (earliest departure, latest departure, latest arrival)."""
    airlines = airlines or []
    info = _field(3, 2, _flight_data(dep, origin, dest, airlines, nonstop, out_hours))
    info += _field(3, 2, _flight_data(ret, dest, origin, airlines, nonstop, ret_hours))
    info += _field(8, 2, b"".join(_varint(1) for _ in range(adults)))  # packed: ADULT x N
    info += _field(9, 0, 1)    # seat: economy
    info += _field(19, 0, 1)   # trip: round trip
    tfs = base64.urlsafe_b64encode(info).decode().rstrip("=")
    return "https://www.google.com/travel/flights/search?" + urlencode({"tfs": tfs, "hl": "zh-TW", "curr": "TWD"})


def window_hours(w) -> tuple[int, int, int]:
    """Whole-hour version of a config Window, matching how Google filters (latest hour inclusive)."""
    return w.depart_earliest // 60, w.depart_latest // 60, w.arrive_latest // 60


def url_for(cfg, origin: str, dest: str, dep: date, ret: date) -> str:
    return google_flights_url(origin, dest, dep, ret, cfg.adults, cfg.airlines, cfg.nonstop_only,
                              window_hours(cfg.outbound), window_hours(cfg.inbound))
