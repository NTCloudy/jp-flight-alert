"""Exact (minute-level) itinerary rules and price classification."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, Window
from .gflights import Itinerary, Leg

LATE_OUT = "去程稍晚抵達"
LATE_RET = "回程稍晚抵達"


def _minutes(dt) -> int:
    return dt.hour * 60 + dt.minute


def _check_direction(legs: list[Leg], w: Window, cfg: Config, label: str, late_flag: str) -> tuple[bool, list[str], str]:
    if not legs:
        return False, [], f"{label}沒有航班"
    if cfg.nonstop_only and len(legs) != 1:
        return False, [], f"{label}不是直飛"
    for leg in legs:
        if leg.airline not in cfg.airlines:
            return False, [], f"{label} {leg.flight} 不是指定的傳統航空"
        if leg.operating and leg.operating not in cfg.airlines:
            return False, [], f"{label} {leg.flight} 實際由 {leg.operating} 執飛"
    first, last = legs[0], legs[-1]
    dep = _minutes(first.dep)
    if not w.depart_earliest <= dep <= w.depart_latest:
        return False, [], f"{label} {first.flight} {first.dep:%H:%M} 起飛不在時段內"
    if last.arr.date() != first.dep.date():
        return False, [], f"{label} {last.flight} 隔天才抵達"
    arr = _minutes(last.arr)
    if arr > w.arrive_latest:
        return False, [], f"{label} {last.flight} {last.arr:%H:%M} 抵達太晚"
    return True, ([late_flag] if arr > w.arrive_ideal else []), ""


def check(it: Itinerary, cfg: Config) -> tuple[bool, list[str], str]:
    ok, f1, why = _check_direction(it.out, cfg.outbound, cfg, "去程", LATE_OUT)
    if not ok:
        return False, [], why
    ok, f2, why = _check_direction(it.ret, cfg.inbound, cfg, "回程", LATE_RET)
    if not ok:
        return False, [], why
    return True, f1 + f2, ""


def best(its: list[Itinerary], cfg: Config) -> tuple[Itinerary, list[str]] | None:
    """Cheapest itinerary that satisfies every rule (ties: fewer warning flags first)."""
    valid = []
    for it in its:
        ok, flags, _ = check(it, cfg)
        if ok:
            valid.append((it.total, len(flags), it, flags))
    if not valid:
        return None
    valid.sort(key=lambda x: (x[0], x[1]))
    return valid[0][2], valid[0][3]


@dataclass
class Verdict:
    category: str | None  # "instant" | "digest" | "anomaly" | None
    discount_pct: int | None = None


def classify(price_pp: float, cfg: Config, route_avg_pp: float | None) -> Verdict:
    discount = None
    if route_avg_pp:
        discount = round((1 - price_pp / route_avg_pp) * 100)
    if price_pp <= cfg.instant_max:
        return Verdict("instant", discount)
    if price_pp <= cfg.digest_max:
        return Verdict("digest", discount)
    if discount is not None and discount >= cfg.anomaly_pct:
        return Verdict("anomaly", discount)
    return Verdict(None, discount)


def anomaly_ceiling(cfg: Config, route_avg_pp: float | None) -> float:
    """Highest per-person price that could still count as 'unusually cheap' for a route."""
    if not route_avg_pp:
        return 0
    return route_avg_pp * (1 - cfg.anomaly_pct / 100)
