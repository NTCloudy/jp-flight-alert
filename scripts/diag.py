"""Diagnostics: raw Google Flights responses (calendar null prices, shopping results) from this machine."""

from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_alert import config as cfgmod  # noqa: E402
from flight_alert.gflights import GoogleFlights  # noqa: E402


def raw_calendar(src: GoogleFlights, origin, dest, nights, start, end):
    from fli.models.google_flights.dates import DateSearchFilters
    from fli.search._urls import with_locale_params
    from fli.search._wire import parse_first_wrb_payload

    f = DateSearchFilters(**src._common(), flight_segments=src._segments(origin, dest, start, start + timedelta(days=nights)),
                          from_date=start.isoformat(), to_date=end.isoformat(), duration=nights)
    url = with_locale_params(src._dates.BASE_URL, "TWD", "zh-TW", "TW")
    r = src._dates.client.post(url=url, data=f"f.req={f.encode()}", impersonate="chrome", allow_redirects=True)
    data = parse_first_wrb_payload(r.text)
    items = data[-1] if isinstance(data, list) and data and isinstance(data[-1], list) else []
    priced = [i for i in items if isinstance(i, list) and len(i) > 2 and i[2]]
    flags = {}
    for i in items:
        if isinstance(i, list) and len(i) > 3:
            flags[str(i[3])] = flags.get(str(i[3]), 0) + 1
    head = json.dumps(data[0], ensure_ascii=False)[:160] if isinstance(data, list) and data else None
    return len(r.text), len(items), len(priced), flags, head


def main():
    cfg = cfgmod.load("config.yaml")
    src = GoogleFlights(cfg, gap=4)
    start, end = cfg.date_range(cfgmod.now_tpe().date())
    for route, k in [("TPE-KIX", 0), ("TPE-KIX", 0), ("TPE-NRT", 1), ("TPE-NRT", 1), ("TPE-OKA", 0), ("TPE-KIX", 1)]:
        o, d = route.split("-")
        s = start + timedelta(days=61 * k)
        e = min(end, s + timedelta(days=60))
        t = time.time()
        size, n, priced, flags, head = raw_calendar(src, o, d, 4, s, e)
        print(f"{route} chunk{k}: bytes={size} items={n} priced={priced} flags={flags} {time.time()-t:.1f}s head={head}", flush=True)

    # shopping: outbound stage only, then full RT
    from fli.models.google_flights.base import SortBy
    from fli.models.google_flights.flights import FlightSearchFilters
    from datetime import date
    for (o, d, dep, ret) in [("TPE", "OKA", start + timedelta(days=30), start + timedelta(days=34)),
                             ("TPE", "NRT", start + timedelta(days=20), start + timedelta(days=24))]:
        f = FlightSearchFilters(**src._common(), flight_segments=src._segments(o, d, dep, ret), sort_by=SortBy.CHEAPEST)
        outs = src._flights._fetch_flights(f, currency="TWD", language="zh-TW", country="TW", capture_session=True)
        print(f"shopping outbound {o}-{d} {dep}: {None if outs is None else len(outs)}", flush=True)
        for x in (outs or [])[:5]:
            leg = x.legs[0]
            print(f"   {leg.airline.name}{leg.flight_number} {leg.departure_datetime:%H:%M}->{leg.arrival_datetime:%H:%M} {x.price}")
        its = src.itineraries(o, d, dep, ret)
        print(f"   itineraries: {None if its is None else len(its)}")
        for it in (its or [])[:5]:
            print(f"   {it.out[0].flight} {it.out[0].dep:%H:%M} / {it.ret[0].flight} {it.ret[0].dep:%H:%M} total={it.total}")
    print("stats", src.stats)


if __name__ == "__main__":
    main()
