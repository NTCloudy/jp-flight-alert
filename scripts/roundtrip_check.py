"""One-off round-trip search with custom time windows (Google Flights, TWD, per person).

python scripts/roundtrip_check.py --route TPE-OKA --dep 2026-11-13 --ret 2026-11-15 \
    --out-window 10:00-20:00 --ret-window 09:00-23:59
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_alert import config as cfgmod  # noqa: E402
from flight_alert.gflights import GoogleFlights  # noqa: E402


def window(s: str) -> cfgmod.Window:
    a, b = (cfgmod.hhmm(x, "window") for x in s.split("-"))
    return cfgmod.Window(a, b, 23 * 60 + 59, 23 * 60 + 59)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--route", default="TPE-OKA")
    ap.add_argument("--dep", required=True)
    ap.add_argument("--ret", required=True)
    ap.add_argument("--out-window", default="00:00-23:59")
    ap.add_argument("--ret-window", default="00:00-23:59")
    ap.add_argument("--max-outbound", type=int, default=8)
    args = ap.parse_args()
    ow, rw = window(args.out_window), window(args.ret_window)
    cfg = dataclasses.replace(cfgmod.load("config.yaml"), outbound=ow, inbound=rw, airlines=[], adults=1)
    g = GoogleFlights(cfg, gap=4)
    ok = lambda legs, w: w.depart_earliest <= legs[0].dep.hour * 60 + legs[0].dep.minute <= w.depart_latest  # noqa: E731
    for route in args.route.split(","):
        o, d = route.strip().split("-")
        its = g.itineraries(o, d, date.fromisoformat(args.dep), date.fromisoformat(args.ret),
                            outbound_ok=lambda legs: ok(legs, ow), max_outbound=args.max_outbound)
        if not its:
            print(f"RT {route} NONE {g.last_note}")
            continue
        seen = set()
        for it in sorted(its, key=lambda x: x.total):
            if not ok(it.ret, rw):
                continue
            fmt = lambda legs: " + ".join(f"{x.flight} {x.origin}{x.dep:%H:%M}->{x.dest}{x.arr:%H:%M}" for x in legs)  # noqa: E731
            line = f"RT {route} | {fmt(it.out)} || {fmt(it.ret)} | NT${int(it.total):,}/人"
            if line not in seen:
                seen.add(line)
                print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
