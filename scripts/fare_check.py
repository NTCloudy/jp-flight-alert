"""One-off fare check: one-way economy fares for specific dates/route/airline (Google Flights, TWD, 1 adult).

python scripts/fare_check.py --route TPE-KIX --dates 2026-10-22,2026-10-23 --airline CX
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_alert import config as cfgmod  # noqa: E402
from flight_alert.gflights import GoogleFlights, _leg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--route", default="TPE-KIX")
    ap.add_argument("--dates", required=True)
    ap.add_argument("--airline", default="CX")
    args = ap.parse_args()
    from fli.models.google_flights.base import FlightSegment, MaxStops, PassengerInfo, SeatType, SortBy, TripType
    from fli.models.google_flights.flights import FlightSearchFilters

    g = GoogleFlights(cfgmod.load("config.yaml"), gap=4)
    A, L = g._Airport, g._Airline
    for route in args.route.split(","):
        run_route(g, A, L, args, route.strip())
    return 0


def run_route(g, A, L, args, route: str) -> None:
    from fli.models.google_flights.base import FlightSegment, MaxStops, PassengerInfo, SeatType, SortBy, TripType
    from fli.models.google_flights.flights import FlightSearchFilters

    o, d = route.split("-")
    if o not in A.__members__ or d not in A.__members__:
        print(f"FARE {route} UNKNOWN AIRPORT")
        return
    for day in args.dates.split(","):
        f = FlightSearchFilters(trip_type=TripType.ONE_WAY, passenger_info=PassengerInfo(adults=1),
                                stops=MaxStops.ANY, seat_type=SeatType.ECONOMY,
                                airlines=None if args.airline.upper() in ("", "ALL", "ANY") else [L[args.airline]],
                                flight_segments=[FlightSegment(departure_airport=[[A[o], 0]],
                                                               arrival_airport=[[A[d], 0]], travel_date=day)],
                                sort_by=SortBy.CHEAPEST)
        try:
            rows = g._flights._fetch_flights(f, capture_session=True, currency="TWD", language="zh-TW", country="TW")
        except Exception as e:  # noqa: BLE001
            print(f"FARE {route} {day} ERROR {type(e).__name__}: {str(e)[:120]}")
            continue
        rows = [r for r in rows or [] if r.legs]
        if not rows:
            print(f"FARE {route} {day} NONE")
        for r in rows:
            legs = [_leg(x) for x in r.legs]
            path = " + ".join(f"{x.flight} {x.origin}{x.dep:%H:%M}->{x.dest}{x.arr:%H:%M}" for x in legs)
            print(f"FARE {route} {day} | stops {len(legs) - 1} | {path} | {'NT$' + format(int(r.price), ',') if r.price else 'no price'}")


if __name__ == "__main__":
    raise SystemExit(main())
