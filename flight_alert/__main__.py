"""CLI: python -m flight_alert {auto,scan,digest,test-email,plan}"""

from __future__ import annotations

import argparse
import logging
import sys

from . import app, store
from . import config as cfgmod
from .airports import airline_name, airport_name


def cmd_plan(cfg: cfgmod.Config) -> int:
    state = store.load_state()
    start, end = cfg.date_range(cfgmod.now_tpe().date())
    print(f"查詢範圍：{start} ～ {end} 出發，玩 {cfg.min_days}–{cfg.max_days} 天（最想 {cfg.prefer_days} 天），{cfg.adults} 人經濟艙")
    print(f"航空公司：{'、'.join(airline_name(a) for a in cfg.airlines)}｜{'只看直飛' if cfg.nonstop_only else '可轉機'}")
    print(f"去程：{cfg.outbound.describe()}")
    print(f"回程：{cfg.inbound.describe()}")
    print(f"通知：每人 ≤ {cfg.instant_max:,} 馬上寄；≤ {cfg.digest_max:,} 或比平常便宜 {cfg.anomaly_pct}% 以上 → "
          f"每天 {cfg.digest_hour}:00 整理" + (f"｜暫停到 {cfg.paused_until}" if cfg.paused_until else ""))
    routes = cfg.routes()
    print(f"航線（{len(routes)} 條）：" + "、".join(f"{airport_name(o)}→{airport_name(d)}" for o, d in routes))
    off = [r for r, v in state.get("routes", {}).items() if v.get("no_service_until")]
    if off:
        print("目前沒有符合條件航班（每週重查）：" + "、".join(off))
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="flight_alert", description="日本傳統航空便宜機票提醒")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_ in [("auto", "排程用：查詢到期的項目、寄即時通知，時間到了寄每日整理"),
                        ("scan", "立即檢查（不管是否到期，先查最舊的資料）"),
                        ("digest", "立即寄出每日整理")]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--dry-run", action="store_true", help="只印出通知，不寄信")
        sp.add_argument("--max-requests", type=int, default=None)
    o = sub.add_parser("ota", help="到 Trip.com／Agoda 核對 Google 最便宜的幾個行程（同航班）")
    o.add_argument("--dry-run", action="store_true", help="只印出通知，不寄信")
    o.add_argument("--limit", type=int, default=5)
    t = sub.add_parser("test-email", help="寄一封測試信")
    t.add_argument("--dry-run", action="store_true")
    sub.add_parser("plan", help="列出目前的條件與航線")
    args = p.parse_args(argv)

    if args.cmd == "test-email":
        return app.test_email(dry_run=args.dry_run)
    try:
        cfg = cfgmod.load(args.config)
    except cfgmod.ConfigError as e:
        print(f"[設定錯誤] {e}", file=sys.stderr)
        return 2
    if args.cmd == "plan":
        return cmd_plan(cfg)
    if args.cmd == "ota":
        from . import ota
        return ota.run(cfg, dry_run=args.dry_run, limit=args.limit)
    return app.run(cfg, mode=args.cmd, dry_run=args.dry_run, max_requests=args.max_requests)


if __name__ == "__main__":
    sys.exit(main())
