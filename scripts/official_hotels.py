"""One-off: live vacancy + price on the Toyoko Inn and APA official sites (headless browser).

python scripts/official_hotels.py --checkin 2026-10-23 --checkout 2026-10-27 --people 2
"""

from __future__ import annotations

import argparse
import re
import urllib.parse

from playwright.sync_api import sync_playwright

AREA = re.compile(r"心斎橋|心齋橋|なんば|難波|日本橋|四ツ橋|長堀|堀江|本町|Shinsaibashi|Namba|Nanba", re.I)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def text(page) -> str:
    return re.sub(r"[ \t]+", " ", page.inner_text("body"))


def toyoko(ctx, a) -> None:
    p = ctx.new_page()
    p.goto("https://www.toyoko-inn.com/hotel_list/", wait_until="domcontentloaded", timeout=90000)
    p.wait_for_timeout(8000)
    codes = {}
    for el in p.locator('a[href*="/search/detail/"]').all():
        href, name = el.get_attribute("href") or "", re.sub(r"\s+", " ", el.inner_text()).strip()
        m = re.search(r"/search/detail/([^/]+)/", href)
        if m and AREA.search(name) and ("大阪" in name or "Osaka" in name):
            codes[m.group(1)] = name
    print(f"TOYOKO_HOTELS {len(codes)}: " + "; ".join(f"{k}={v}" for k, v in codes.items()))
    for code, name in codes.items():
        q = urllib.parse.urlencode({"hotelCodes": code, "checkinDate": a.checkin, "checkoutDate": a.checkout,
                                    "numberOfPeople": a.people, "numberOfRoom": 1})
        url = f"https://www.toyoko-inn.com/search/result/room_plan/?{q}&hotelCode={code}"
        try:
            p.goto(url, wait_until="domcontentloaded", timeout=90000)
            p.wait_for_timeout(9000)
            t = text(p)
            prices = re.findall(r"[¥￥]\s?[\d,]{4,}|[\d,]{4,}\s?円", t)
            print("   PAGE: " + re.sub(r"\s+", " ", t)[:1500])
            rooms = re.findall(r"(?:ダブル|ツイン|シングル|Double|Twin|ハリウッド|エコノミー|デラックス)[^\n]{0,40}", t)
            sold = "満室" in t or "空室なし" in t
            print(f"TOYOKO | {name} | {url}\n   prices: {prices[:12]}\n   rooms: {list(dict.fromkeys(rooms))[:8]}\n   満室: {sold}")
            p.screenshot(path=f"official_toyoko_{code}.png", full_page=True)
        except Exception as e:  # noqa: BLE001
            print(f"TOYOKO | {name} | error {e}")


def apa(ctx, a) -> None:
    p = ctx.new_page()
    try:
        r = p.goto("https://www.apahotel.com/", wait_until="domcontentloaded", timeout=90000)
        p.wait_for_timeout(4000)
        print(f"APA home status {r.status if r else '?'} title {p.title()}")
        links = {el.get_attribute("href") for el in p.locator("a[href]").all()}
        print("APA links: " + " ".join(sorted(h for h in links if h and re.search(r"search|reserv|osaka|kansai|area", h, re.I))[:40]))
        p.screenshot(path="official_apa_home.png", full_page=False)
        for u in ("https://www.apahotel.com/hotel/kansai/osaka/", "https://www.apahotel.com/hotel/kansai/"):
            r = p.goto(u, wait_until="domcontentloaded", timeout=90000)
            p.wait_for_timeout(3000)
            names = [re.sub(r"\s+", " ", el.inner_text()).strip() for el in p.locator("a").all()]
            hits = [n for n in names if AREA.search(n)]
            hrefs = [el.get_attribute("href") for el in p.locator("a").all() if AREA.search(el.inner_text() or "")]
            print(f"APA list {u} status {r.status if r else '?'}: {list(dict.fromkeys(hits))[:20]}")
            print("   hrefs: " + " ".join(list(dict.fromkeys(h for h in hrefs if h))[:20]))
    except Exception as e:  # noqa: BLE001
        print(f"APA error {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkin", required=True)
    ap.add_argument("--checkout", required=True)
    ap.add_argument("--people", type=int, default=2)
    a = ap.parse_args()
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(locale="ja-JP", timezone_id="Asia/Tokyo", viewport={"width": 1280, "height": 2000},
                            user_agent=UA)
        try:
            toyoko(ctx, a)
        except Exception as e:  # noqa: BLE001
            print(f"TOYOKO error {e}")
        apa(ctx, a)
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
