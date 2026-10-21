"""One-off hotel search: Booking.com results in a real (headless) browser, printed as text.

python scripts/hotel_search.py --area "Shinsaibashi, Osaka" --checkin 2026-10-23 --checkout 2026-10-27 --adults 2
Filters: free cancellation, review score 8+; prints name, total price, score, distance, cancellation text,
then opens the best few to check bathtub / view / room type.
"""

from __future__ import annotations

import argparse
import re
import urllib.parse

from playwright.sync_api import sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", default="Shinsaibashi, Osaka")
    ap.add_argument("--checkin", required=True)
    ap.add_argument("--checkout", required=True)
    ap.add_argument("--adults", type=int, default=2)
    ap.add_argument("--max-total", type=int, default=16000, help="TWD, all nights")
    ap.add_argument("--details", type=int, default=8)
    args = ap.parse_args()
    q = urllib.parse.urlencode({
        "ss": args.area, "checkin": args.checkin, "checkout": args.checkout, "group_adults": args.adults,
        "no_rooms": 1, "group_children": 0, "selected_currency": "TWD", "lang": "zh-tw",
        "nflt": "fc=2;review_score=80", "order": "price"})
    url = f"https://www.booking.com/searchresults.zh-tw.html?{q}"
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(locale="zh-TW", timezone_id="Asia/Taipei", viewport={"width": 1280, "height": 2400},
                            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) Chrome/126 Safari/537.36")
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        for _ in range(4):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)
        cards = page.locator('[data-testid="property-card"]')
        n = cards.count()
        print(f"CARDS {n}")
        rows = []
        for i in range(n):
            c = cards.nth(i)
            txt = re.sub(r"\s+", " ", c.inner_text())
            name = c.locator('[data-testid="title"]').first.inner_text() if c.locator('[data-testid="title"]').count() else "?"
            link = c.locator('a[data-testid="title-link"]').first.get_attribute("href") if c.locator('a[data-testid="title-link"]').count() else ""
            prices = [int(x.replace(",", "")) for x in re.findall(r"TWD\s?([\d,]{4,})", txt)]
            total = prices[-1] if prices else None
            score = re.search(r"評分\s*([\d.]+)|([\d.]+)\s*(?:很讚|好極了|傑出|超讚|非常好)", txt)
            dist = re.search(r"距(?:離)?[^ ]*中心[^ ]*\s*[\d.]+\s*(?:公里|公尺)|[\d.]+\s*(?:公里|公尺)[^ ]*", txt)
            rows.append((total, name, txt, link))
            print(f"HOTEL | {name} | total TWD {total} | "
                  f"{(score.group(1) or score.group(2)) if score else '?'} | {dist.group(0) if dist else ''} | "
                  f"{'免費取消' if '免費取消' in txt else ''} | {'到店付款' if '住宿方付款' in txt or '到店' in txt else ''}")
        page.screenshot(path="hotels_list.png", full_page=False)
        picked = [r for r in rows if r[0] and r[0] <= args.max_total][:args.details]
        for total, name, _, link in picked:
            if not link:
                continue
            d = ctx.new_page()
            try:
                d.goto(link, wait_until="domcontentloaded", timeout=60000)
                d.wait_for_timeout(4000)
                body = re.sub(r"\s+", " ", d.inner_text("body"))
                feats = [k for k in ("浴缸", "市景", "城市景觀", "景觀", "窗戶", "超市", "便利商店") if k in body]
                rooms = re.findall(r"(?:雙人房|雙床房|豪華|標準|高級)[^ ]{0,20}", body)[:6]
                cancel = re.findall(r"[^。]{0,30}(?:免費取消)[^。]{0,40}", body)[:3]
                station = re.findall(r"[^ ]{0,12}(?:站)\s*[\d.]+\s*(?:公尺|公里)", body)[:4]
                print(f"DETAIL | {name} | TWD {total} | 設施: {','.join(feats)} | 房型: {'; '.join(dict.fromkeys(rooms))} "
                      f"| 車站: {'; '.join(station)} | 取消: {' / '.join(cancel)}")
            except Exception as e:  # noqa: BLE001
                print(f"DETAIL | {name} | error {e}")
            finally:
                d.close()
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
