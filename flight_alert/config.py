"""Load and validate config.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .airports import FULL_SERVICE_AIRLINES, JAPAN_AIRPORTS, LOW_COST_AIRLINES, SONGSHAN_DESTINATIONS, TAIWAN_AIRPORTS

TPE_TZ = timezone(timedelta(hours=8))

DEFAULT_DESTINATIONS = [
    "NRT", "HND", "KIX", "NGO", "FUK", "CTS", "OKA", "SDJ", "HKD", "AOJ", "KMQ", "TOY", "HIJ",
    "OKJ", "TAK", "MYJ", "KMJ", "KOJ", "KMI", "FSZ", "IBR", "KIJ", "HNA", "AXT", "ISG", "UKB", "KKJ",
]
DEFAULT_AIRLINES = ["CI", "BR", "JX", "AE", "JL", "NH", "CX"]
DEFAULT_LCC_AIRLINES = list(LOW_COST_AIRLINES)
# Estimated price of one 20 kg checked bag, per person, per flight, bought online with the ticket (TWD).
# Sources: airline / travel-site price lists (2025-26). Carriers not listed use DEFAULT_LCC_BAG_FEE.
DEFAULT_LCC_BAG_FEES = {
    "IT": 950, "MM": 950, "GK": 950, "JQ": 950, "TR": 950,
    "D7": 950, "AK": 950, "FD": 950, "Z2": 950, "VJ": 600, "VZ": 600,
}
DEFAULT_LCC_BAG_FEE = 1200  # unknown / unlisted low-cost carrier: conservative (higher) estimate


class ConfigError(ValueError):
    pass


def hhmm(value, name: str) -> int:
    """'14:00' -> minutes after midnight. (YAML 1.1 reads an unquoted 14:00 as the base-60
    integer 840, which is conveniently the same number of minutes.)"""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 24 * 60:
        return value
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ConfigError(f"{name} 時間格式要像 14:00，目前是 {value!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Window:
    """Local-time constraints for one direction of the trip (minutes after midnight)."""

    depart_earliest: int
    depart_latest: int
    arrive_ideal: int
    arrive_latest: int

    def describe(self) -> str:
        return (f"{fmt_hhmm(self.depart_earliest)}–{fmt_hhmm(self.depart_latest)} 起飛，"
                f"{fmt_hhmm(self.arrive_ideal)} 前抵達（最晚 {fmt_hhmm(self.arrive_latest)}）")


@dataclass
class LccConfig:
    """Separate low-cost-carrier alert: (fare + estimated 20 kg bag both ways) per person < max_total."""

    enabled: bool = True
    max_total: int = 5000          # per person, round trip, taxes + estimated checked bag included
    bag_kg: int = 20               # informational (fees below are for this weight)
    airlines: list[str] = field(default_factory=lambda: list(DEFAULT_LCC_AIRLINES))
    bag_fees: dict = field(default_factory=lambda: dict(DEFAULT_LCC_BAG_FEES))
    bag_fee_default: int = DEFAULT_LCC_BAG_FEE
    renotify_drop: int = 500

    def bag_fee(self, airline: str) -> int:
        return int(self.bag_fees.get(airline, self.bag_fee_default))

    @property
    def min_bag_round_trip(self) -> int:
        fees = [self.bag_fee(a) for a in self.airlines] or [self.bag_fee_default]
        return 2 * min(fees)


@dataclass
class Config:
    instant_max: int = 8000
    digest_max: int = 10000
    anomaly_pct: int = 20
    digest_hour: int = 8
    digest_enabled: bool = False   # daily digest email (super-cheap deals are emailed right away anyway)
    renotify_drop: int = 300
    paused_until: date | None = None
    interval_hours: float = 3
    origins: list[str] = field(default_factory=lambda: ["TPE", "TSA"])
    destinations: list[str] = field(default_factory=lambda: list(DEFAULT_DESTINATIONS))
    min_days: int = 4
    max_days: int = 8
    prefer_days: int = 5
    lead_days: int = 2
    horizon_days: int = 180
    adults: int = 2
    nonstop_only: bool = True
    airlines: list[str] = field(default_factory=lambda: list(DEFAULT_AIRLINES))
    outbound: Window = Window(10 * 60, 14 * 60, 18 * 60, 18 * 60 + 30)
    inbound: Window = Window(13 * 60, 15 * 60, 19 * 60, 20 * 60)
    promo_enabled: bool = True
    muted: list[str] = field(default_factory=list)
    lcc: LccConfig = field(default_factory=LccConfig)

    # ---- derived helpers -------------------------------------------------
    @property
    def durations(self) -> list[int]:
        """Nights between departure and return dates (trip days - 1)."""
        return [d - 1 for d in range(self.min_days, self.max_days + 1)]

    def routes(self) -> list[tuple[str, str]]:
        out = []
        for o in self.origins:
            for d in self.destinations:
                if o == "TSA" and d not in SONGSHAN_DESTINATIONS:
                    continue
                out.append((o, d))
        return out

    def is_paused(self, today: date) -> bool:
        return self.paused_until is not None and today <= self.paused_until

    def date_range(self, today: date) -> tuple[date, date]:
        start = today + timedelta(days=self.lead_days)
        return start, today + timedelta(days=self.horizon_days)


def _window(raw: dict, default: Window, name: str) -> Window:
    raw = raw or {}
    w = Window(
        hhmm(raw.get("depart_earliest", fmt_hhmm(default.depart_earliest)), f"{name}.depart_earliest"),
        hhmm(raw.get("depart_latest", fmt_hhmm(default.depart_latest)), f"{name}.depart_latest"),
        hhmm(raw.get("arrive_ideal", fmt_hhmm(default.arrive_ideal)), f"{name}.arrive_ideal"),
        hhmm(raw.get("arrive_latest", fmt_hhmm(default.arrive_latest)), f"{name}.arrive_latest"),
    )
    if w.depart_earliest > w.depart_latest:
        raise ConfigError(f"{name}：最早起飛時間不能晚於最晚起飛時間")
    if w.arrive_ideal > w.arrive_latest:
        raise ConfigError(f"{name}：理想抵達時間不能晚於最晚抵達時間")
    return w


def _codes(values, allowed: dict, name: str) -> list[str]:
    out = []
    for v in values or []:
        code = str(v).strip().upper()
        if code not in allowed:
            raise ConfigError(f"{name} 裡的 {v!r} 不認得")
        if code not in out:
            out.append(code)
    return out


def _lcc(raw) -> LccConfig:
    raw = raw or {}
    base = LccConfig()
    fees = dict(base.bag_fees)
    for k, v in (raw.get("bag_fees") or {}).items():
        fees[str(k).strip().upper()] = int(v)
    lcc = LccConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        max_total=int(raw.get("max_total", base.max_total)),
        bag_kg=int(raw.get("bag_kg", base.bag_kg)),
        airlines=_codes(raw.get("airlines", base.airlines), LOW_COST_AIRLINES, "lcc.airlines"),
        bag_fees=fees,
        bag_fee_default=int(raw.get("bag_fee_default", base.bag_fee_default)),
        renotify_drop=int(raw.get("renotify_drop", base.renotify_drop)),
    )
    if lcc.enabled and not lcc.airlines:
        raise ConfigError("lcc.airlines 至少要選一家廉航")
    if lcc.enabled and lcc.max_total <= lcc.min_bag_round_trip:
        raise ConfigError("lcc.max_total 要高於來回行李費")
    return lcc


def parse(data: dict) -> Config:
    data = data or {}
    alerts = data.get("alerts") or {}
    trip = data.get("trip") or {}
    sched = data.get("schedule") or {}
    promo = data.get("promo") or {}
    base = Config()

    paused = str(alerts.get("paused_until") or "").strip()
    try:
        paused_until = date.fromisoformat(paused) if paused else None
    except ValueError as e:
        raise ConfigError(f"alerts.paused_until 要寫成 YYYY-MM-DD，目前是 {paused!r}") from e

    cfg = Config(
        instant_max=int(alerts.get("instant_max", base.instant_max)),
        digest_max=int(alerts.get("digest_max", base.digest_max)),
        anomaly_pct=int(alerts.get("anomaly_pct", base.anomaly_pct)),
        digest_hour=int(alerts.get("digest_hour", base.digest_hour)),
        digest_enabled=bool(alerts.get("digest_enabled", base.digest_enabled)),
        renotify_drop=int(alerts.get("renotify_drop", base.renotify_drop)),
        paused_until=paused_until,
        interval_hours=float(sched.get("interval_hours", base.interval_hours)),
        origins=_codes(trip.get("origins", base.origins), TAIWAN_AIRPORTS, "trip.origins"),
        destinations=_codes(trip.get("destinations", base.destinations), JAPAN_AIRPORTS, "trip.destinations"),
        min_days=int(trip.get("min_days", base.min_days)),
        max_days=int(trip.get("max_days", base.max_days)),
        prefer_days=int(trip.get("prefer_days", base.prefer_days)),
        lead_days=int(trip.get("lead_days", base.lead_days)),
        horizon_days=int(trip.get("horizon_days", base.horizon_days)),
        adults=int(trip.get("adults", base.adults)),
        nonstop_only=bool(trip.get("nonstop_only", base.nonstop_only)),
        airlines=_codes(trip.get("airlines", base.airlines), FULL_SERVICE_AIRLINES, "trip.airlines"),
        outbound=_window(data.get("outbound"), base.outbound, "outbound"),
        inbound=_window(data.get("inbound"), base.inbound, "inbound"),
        promo_enabled=bool(promo.get("enabled", True)),
        muted=[str(x) for x in (data.get("muted") or [])],
        lcc=_lcc(data.get("lcc")),
    )
    if not cfg.origins or not cfg.destinations or not cfg.airlines:
        raise ConfigError("出發機場、目的地、航空公司都至少要選一個")
    if not 1 <= cfg.min_days <= cfg.max_days <= 30:
        raise ConfigError("天數設定不合理（min_days ≤ max_days，且最多 30 天）")
    if not 1 <= cfg.adults <= 9:
        raise ConfigError("人數要在 1～9 位之間")
    if cfg.instant_max > cfg.digest_max:
        raise ConfigError("「超便宜」價格不能高於「每日整理」價格")
    if not 0 <= cfg.digest_hour <= 23:
        raise ConfigError("digest_hour 要在 0～23 之間")
    if not 1 <= cfg.horizon_days <= 300:
        raise ConfigError("horizon_days 要在 1～300 之間（Google Flights 最多查約 300 天內）")
    return cfg


def load(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        return parse(yaml.safe_load(f))


def now_tpe() -> datetime:
    return datetime.now(TPE_TZ)
