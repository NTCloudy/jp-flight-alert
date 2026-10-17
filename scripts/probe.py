"""Feasibility probe: can this machine (e.g. a GitHub Actions runner) query Google Flights?

python scripts/probe.py --gap 6 --max-calls 30
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_alert import config as cfgmod  # noqa: E402
from flight_alert import rules  # noqa: E402
from flight_alert.gflights import GoogleFlights, RateLimited  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=6)
    ap.add_argument("--max-calls", type=int, default=30)
    ap.add_argument("--routes", default="TPE-NRT,TPE-KIX,TPE-OKA,TSA-HND,TPE-CTS,TPE-FUK,TPE-NGO,TPE-SDJ,TPE-KMJ,TPE-HKD")
    args = ap.parse_args()

    cfg = cfgmod.load("config.yaml")
    src = GoogleFlights(cfg, gap=args.gap)
    start, end = cfg.date_range(cfgmod.now_tpe().date())
    t0 = time.time()
    ok = calls = 0
    best = None
    try:
        for route in args.routes.split(","):
            o, d = route.split("-")
            for k in range(3):
                if calls >= args.max_calls:
                    raise StopIteration
                s = start + timedelta(days=61 * k)
                if s > end:
                    break
                calls += 1
                t = time.time()
                prices = src.calendar(o, d, 4, s, end)
                dt = time.time() - t
                if prices is None:
                    print(f"[{time.time()-t0:6.1f}s] {route} chunk{k}: FAILED ({dt:.1f}s)", flush=True)
                    continue
                ok += 1
                if prices:
                    (dep, ret), total = min(prices.items(), key=lambda kv: kv[1])
                    print(f"[{time.time()-t0:6.1f}s] {route} chunk{k}: {len(prices)} 天有價格，最低每人 {total/cfg.adults:,.0f}（{dep}~{ret}）", flush=True)
                    if best is None or total < best[2]:
                        best = (o, d, total, dep, ret)
                else:
                    print(f"[{time.time()-t0:6.1f}s] {route} chunk{k}: 沒有符合條件的航班", flush=True)
    except StopIteration:
        pass
    except RateLimited as e:
        print(f"!!! RATE LIMITED after {ok} successful calls / {time.time()-t0:.0f}s: {e}", flush=True)
    if best and not src.stats.rate_limited:
        o, d, total, dep, ret = best
        its = src.itineraries(o, d, dep, ret)
        print(f"itineraries {o}-{d} {dep}~{ret}: {None if its is None else len(its)} 組")
        for it in (its or [])[:6]:
            okk, flags, why = rules.check(it, cfg)
            print(f"   {it.out[0].flight} {it.out[0].dep:%H:%M}->{it.out[-1].arr:%H:%M} / "
                  f"{it.ret[0].flight} {it.ret[0].dep:%H:%M}->{it.ret[-1].arr:%H:%M}  每人 {it.total/cfg.adults:,.0f}  "
                  f"{'OK ' + ','.join(flags) if okk else 'X ' + why}")
    print(f"SUMMARY ok={ok} calls={calls} requests={src.stats.requests} failures={src.stats.failures} "
          f"rate_limited={src.stats.rate_limited} elapsed={time.time()-t0:.0f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
