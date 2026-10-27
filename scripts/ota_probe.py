"""Feasibility probe: can a headless browser read flight prices from Trip.com and Agoda?

Opens each site's round-trip search page, records every JSON response that looks like flight results,
and reports: HTTP status, bot-check / captcha signs, how many itineraries were found and the cheapest ones.
Screenshots and raw JSON go to out/ota_probe/ (uploaded as a workflow artifact).

python scripts/ota_probe.py --route TPE-NRT --depart 2026-12-10 --return 2026-12-14
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("out/ota_probe")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
BOT_SIGNS = re.compile(r"captcha|verify you are human|unusual traffic|access denied|robot|驗證|人機|blocked", re.I)
CITY = {"NRT": "tyo", "HND": "tyo", "KIX": "osa", "FUK": "fuk", "CTS": "spk", "OKA": "oka", "NGO": "ngo"}


def trip_url(o: str, d: str, dep: str, ret: str, adults: int) -> str:
    return (f"https://tw.trip.com/flights/showfarefirst?dcity={o.lower()}&acity={d.lower()}&ddate={dep}"
            f"&rdate={ret}&triptype=rt&class=y&quantity={adults}&searchboxarg=t&nonstoponly=on"
            f"&locale=zh-TW&curr=TWD")


def agoda_url(o: str, d: str, dep: str, ret: str, adults: int) -> str:
    return (f"https://www.agoda.com/zh-tw/flights/results?departureFrom={o}&departureFromType=1&arrivalTo={d}"
            f"&arrivalToType=1&departDate={dep}&returnDate={ret}&searchType=2&cabinType=Economy"
            f"&adults={adults}&children=0&infants=0&sort=8&currency=TWD")


def looks_like_flights(text: str) -> bool:
    t = text[:400000]
    return (("price" in t.lower() or "fare" in t.lower())
            and any(k in t for k in ("flightNo", "flightNumber", "airlineCode", "carrierCode", "marketingCarrier",
                                     "departureTime", "departDateTime", "segments", "itinerar")))


def prices_in(obj, out: list, depth: int = 0) -> None:
    """Collect plausible TWD totals (number fields named like price/amount/total) from any JSON shape."""
    if depth > 14 or len(out) > 3000:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and re.search(r"price|amount|total|fare", k, re.I) and 1500 <= v <= 200000:
                out.append((k, v))
            else:
                prices_in(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:400]:
            prices_in(v, out, depth + 1)


def probe(pw, name: str, url: str, wait: int) -> dict:
    browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(user_agent=UA, locale="zh-TW", timezone_id="Asia/Taipei",
                              viewport={"width": 1366, "height": 900})
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page = ctx.new_page()
    hits, statuses = [], []

    def on_response(r):
        try:
            statuses.append((r.status, r.url[:140]))
            if "json" not in (r.headers.get("content-type") or ""):
                return
            body = r.text()
            if looks_like_flights(body):
                hits.append({"url": r.url[:300], "status": r.status, "size": len(body), "body": body})
        except Exception:
            pass

    page.on("response", on_response)
    res = {"site": name, "url": url}
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        res["status"] = resp.status if resp else None
        deadline = time.time() + wait
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            page.mouse.wheel(0, 600)
        html = page.content()
        text = page.inner_text("body")[:20000]
        res["title"] = page.title()
        res["bot_check"] = bool(BOT_SIGNS.search(page.title() + text[:3000]))
        res["page_prices"] = sorted({int(p.replace(",", "")) for p in re.findall(r"(?:TWD|NT\$|\$)\s?([\d,]{4,7})", text)
                                     if 1500 <= int(p.replace(",", "")) <= 200000})[:15]
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
        (OUT / f"{name}.html").write_text(html, encoding="utf-8")
        (OUT / f"{name}_text.txt").write_text(text, encoding="utf-8")
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    res["json_hits"] = []
    for i, h in enumerate(hits):
        (OUT / f"{name}_json_{i}.json").write_text(h["body"], encoding="utf-8")
        found = []
        try:
            prices_in(json.loads(h["body"]), found)
        except Exception:
            pass
        vals = sorted({int(v) for _, v in found})
        res["json_hits"].append({"url": h["url"], "status": h["status"], "size": h["size"],
                                 "price_fields": sorted({k for k, _ in found})[:10], "lowest": vals[:8]})
    res["blocked_statuses"] = [s for s in statuses if s[0] in (403, 429, 432)][:10]
    browser.close()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--route", default="TPE-NRT")
    ap.add_argument("--depart", default="2026-12-10")
    ap.add_argument("--return", dest="ret", default="2026-12-14")
    ap.add_argument("--adults", type=int, default=2)
    ap.add_argument("--wait", type=int, default=35, help="seconds to let results load")
    ap.add_argument("--sites", default="trip,agoda")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    o, d = args.route.split("-")
    urls = {"trip": trip_url(o, CITY.get(d, d), args.depart, args.ret, args.adults),
            "agoda": agoda_url(o, d, args.depart, args.ret, args.adults)}
    results = []
    with sync_playwright() as pw:
        for name in args.sites.split(","):
            r = probe(pw, name, urls[name], args.wait)
            results.append(r)
            print(f"\n===== {name} =====")
            print(json.dumps(r, ensure_ascii=False, indent=1))
    (OUT / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n===== verdict =====")
    for r in results:
        ok = any(h["lowest"] for h in r["json_hits"]) or bool(r.get("page_prices"))
        print(f"{r['site']}: {'✅ prices readable' if ok else '❌ no prices'}"
              f" | bot_check={r.get('bot_check')} | json_hits={len(r['json_hits'])} | page_prices={r.get('page_prices', [])[:5]}"
              f" | blocked={r.get('blocked_statuses')} | error={r.get('error', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
