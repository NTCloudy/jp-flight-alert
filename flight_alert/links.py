"""Google Flights deep links (the `tfs` protobuf parameter used by google.com/travel/flights)."""

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


def _flight_data(day: date, origin: str, dest: str, airlines: list[str], nonstop: bool) -> bytes:
    msg = _str(2, day.isoformat())
    if nonstop:
        msg += _field(5, 0, 0)
    for a in airlines:
        msg += _str(6, a)
    msg += _field(13, 2, _str(2, origin))
    msg += _field(14, 2, _str(2, dest))
    return msg


def google_flights_url(origin: str, dest: str, dep: date, ret: date, adults: int = 2,
                       airlines: list[str] | None = None, nonstop: bool = True) -> str:
    """Round-trip economy search link with passengers, nonstop and airline filters pre-applied."""
    airlines = airlines or []
    info = _field(3, 2, _flight_data(dep, origin, dest, airlines, nonstop))
    info += _field(3, 2, _flight_data(ret, dest, origin, airlines, nonstop))
    info += _field(8, 2, b"".join(_varint(1) for _ in range(adults)))  # packed: ADULT x N
    info += _field(9, 0, 1)    # seat: economy
    info += _field(19, 0, 1)   # trip: round trip
    tfs = base64.urlsafe_b64encode(info).decode().rstrip("=")
    return "https://www.google.com/travel/flights?" + urlencode({"tfs": tfs, "hl": "zh-TW", "curr": "TWD"})
