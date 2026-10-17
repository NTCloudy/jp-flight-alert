"""Open the Google Flights links for the current deals in a real browser and print what is shown,
to check that the link reproduces our search (dates, passengers, nonstop, airlines, time windows).

python scripts/linkcheck.py   (needs: pip install playwright && python -m playwright install chromium)
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

from flight_alert import config, links  # noqa: E402


def main() -> int:
    cfg = config.load("config.yaml")
    deals = json.load(open("data/latest.json", encoding="utf-8")).get("deals", [])[:3]
    if not deals:
        print("no deals in data/latest.json")
        return 0
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(locale="zh-TW", timezone_id="Asia/Taipei", viewport={"width": 1280, "height": 2200})
        for i, d in enumerate(deals):
            url = links.url_for(cfg, d["origin"], d["dest"], date.fromisoformat(d["dep"]), date.fromisoformat(d["ret"]))
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(9000)
            print(f"===== {d['key']}  ours: 去 {d['out']['flight']} {d['out']['dep']}→{d['out']['arr']} / "
                  f"回 {d['back']['flight']} {d['back']['dep']}→{d['back']['arr']}  total NT${d['price_total']:,}")
            print("url:", page.url[:160])
            lines = [ln.strip() for ln in page.inner_text("body").splitlines() if ln.strip()]
            print("\n".join(lines[:160]))
            page.screenshot(path=f"linkcheck_{i}_{d['dest']}.png", full_page=False)
            page.close()
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
