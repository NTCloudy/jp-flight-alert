"""Cross-check Google Flights deals on Trip.com and Agoda with a headless browser.

Only the few cheapest Google deals are checked (a browser search takes ~40 s), and only the *same* flights
count: both sites list outbound flights first, so we pick the deal's outbound flight, then read the return list
and take the price of the deal's return flight. Prices are per person, round trip, taxes included (TWD), like
Google's. Anything that cannot be matched exactly is reported as "not found" instead of guessed.

Results live in data/ota.json; deals in latest.json get an `ota` field, emails and the console show it.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime

from . import store
from .config import Config, now_tpe

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
SITES = {"trip": "Trip.com", "agoda": "Agoda"}
# Trip.com searches by city: both Tokyo / Osaka airports share one city code
TRIP_CITY = {"NRT": "tyo", "HND": "tyo", "KIX": "osa", "UKB": "ukb", "CTS": "spk", "OKA": "oka"}
RESULT_TTL_HOURS = 30          # older checks are not shown any more
CHEAPER_BY = 300               # OTA must beat Google by this much per person to be called cheaper
SANE_RATIO = (0.6, 1.8)        # an OTA price outside 60%-180% of Google's for the same flights is a parsing error
DATA_VERSION = 2               # v1 stored Agoda prices that were really USD (GitHub runners are in the US)


class SiteError(RuntimeError):
    """The site did not answer as expected (blocked, changed, timed out)."""


# ------------------------------------------------------------------ URLs
def trip_url(o: str, d: str, dep: str, ret: str, adults: int) -> str:
    return (f"https://tw.trip.com/flights/showfarefirst?dcity={TRIP_CITY.get(o, o).lower()}"
            f"&acity={TRIP_CITY.get(d, d).lower()}&ddate={dep}&rdate={ret}&triptype=rt&class=y&quantity={adults}"
            f"&searchboxarg=t&nonstoponly=on&locale=zh-TW&curr=TWD")


def agoda_url(o: str, d: str, dep: str, ret: str, adults: int) -> str:
    return (f"https://www.agoda.com/zh-tw/flights/results?departureFrom={o}&departureFromType=1&arrivalTo={d}"
            f"&arrivalToType=1&departDate={dep}&returnDate={ret}&searchType=2&cabinType=Economy"
            f"&adults={adults}&children=0&infants=0&sort=8&currency=TWD&currencyCode=TWD")


def search_url(site: str, deal: dict) -> str:
    fn = trip_url if site == "trip" else agoda_url
    return fn(deal["origin"], deal["dest"], deal["dep"], deal["ret"], int(deal.get("adults", 2)))


# ------------------------------------------------------------------ parsing (pure, unit-tested)
def split_flight(flight: str) -> tuple[str, str]:
    """"JX870" -> ("JX", "870"); "CI 104" -> ("CI", "104")."""
    m = re.match(r"\s*([A-Z0-9]{2})\s*0*(\d+)", flight or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def same_flight(airline: str, number: str, want: str) -> bool:
    a, n = split_flight(want)
    return bool(a) and airline == a and str(number).lstrip("0") == n


def trip_options(body: str) -> list[dict]:
    """Flight options from a Trip.com FlightListSearch(SSE) answer: one entry per listed flight, with the
    round-trip price per person (outbound list: cheapest return; return list: this exact combination)."""
    docs = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:]
        if line.startswith("{"):
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    out = []
    for d in docs:
        names = {a.get("code"): a.get("name") for a in d.get("airlineList") or [] if isinstance(a, dict)}
        for it in d.get("itineraryList") or []:
            try:
                segs = it["journeyList"][0]["transSectionList"]
                price = it["policies"][0]["price"]
                pp = price.get("averagePrice") or (price.get("adult") or {}).get("totalPrice")
            except (KeyError, IndexError, TypeError):
                continue
            if len(segs) != 1 or not pp:  # nonstop only
                continue
            f = segs[0].get("flightInfo") or {}
            code = f.get("airlineCode") or ""
            no = re.sub(r"^[A-Z0-9]{2}", "", f.get("flightNo") or "").lstrip("0")
            out.append({"airline": code, "number": no, "airline_name": names.get(code) or f.get("airlineName") or "",
                        "dep": segs[0]["departDateTime"][11:16], "arr": segs[0]["arriveDateTime"][11:16],
                        "price_pp": round(float(pp))})
    return out


def agoda_options(body: str) -> tuple[list[dict], bool, bool]:
    """(options, polling completed, a flight is already selected) from an Agoda flights-bff search answer.
    Prices that are not in NT$ (Agoda picks the currency from the visitor's IP) are skipped."""
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        return [], False, False
    data = d.get("data") or {}
    resp = data.get("response") or {}
    out = []
    for it in (resp.get("content") or {}).get("items") or []:
        try:
            segs = it["slice"]["segments"]
            amount = it["totalPrice"]["priceAfterDiscount"]["amount"]
            symbol = it["totalPrice"]["priceAfterDiscount"].get("currencySymbol", "")
        except (KeyError, TypeError):
            continue
        if len(segs) != 1 or symbol not in ("NT$", "TWD", "NT"):
            continue
        s = segs[0]
        out.append({"airline": s["airline"]["code"], "number": str(s["flightNumber"]).lstrip("0"),
                    "airline_name": s["airline"].get("name", ""), "dep": s["departure"]["rawDate"][11:16],
                    "arr": s["arrival"]["rawDate"][11:16], "price_pp": int(re.sub(r"\D", "", amount) or 0)})
    return out, bool((data.get("polling") or {}).get("completed")), bool(resp.get("selectedSlices"))


def find(options: list[dict], leg: dict) -> dict | None:
    return next((o for o in options if same_flight(o["airline"], o["number"], leg.get("flight", ""))), None)


# ------------------------------------------------------------------ browser
def _wait(page, cond, seconds: float, what: str):
    deadline = time.time() + seconds
    while time.time() < deadline:
        v = cond()
        if v:
            return v
        page.wait_for_timeout(1000)
    raise SiteError(f"等不到{what}（{int(seconds)} 秒）")


def _keep(response, needle: str, bodies: list[str]) -> None:
    if needle in response.url:
        try:
            bodies.append(response.text())
        except Exception:  # noqa: BLE001 - aborted / redirected responses have no body
            pass


def _card(page, opt: dict, button: str | None = None):
    """The smallest element that shows this flight's airline name, departure and arrival time."""
    loc = page.locator("div").filter(has_text=opt["dep"]).filter(has_text=opt["arr"])
    if opt.get("airline_name"):
        loc = loc.filter(has_text=opt["airline_name"])
    if button:
        loc = loc.filter(has=page.get_by_role("button", name=button))
    for _ in range(12):  # results render lazily: scroll until the card exists
        if loc.count():
            return loc.last
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(700)
    raise SiteError(f"找不到 {opt['airline']}{opt['number']} 的卡片")


def check_trip(page, deal: dict) -> dict:
    bodies: list[str] = []
    page.on("response", lambda r: _keep(r, "FlightListSearch", bodies))
    page.goto(search_url("trip", deal), wait_until="domcontentloaded", timeout=60000)
    _wait(page, lambda: bodies and trip_options(bodies[-1]), 50, "Trip.com 去程清單")
    page.wait_for_timeout(3000)  # the list keeps refining for a moment
    outs = trip_options(bodies[-1])
    out = find(outs, deal["out"])
    if not out:
        return {"status": "not_found", "note": f"Trip.com 沒有 {deal['out']['flight']}"}
    n = len(bodies)
    _card(page, out, "選取").get_by_role("button", name="選取").first.click(timeout=15000)
    _wait(page, lambda: len(bodies) > n and trip_options(bodies[-1]), 50, "Trip.com 回程清單")
    page.wait_for_timeout(2000)
    back = find(trip_options(bodies[-1]), deal["back"])
    if not back:
        return {"status": "not_found", "note": f"Trip.com 沒有 {deal['back']['flight']}（去程 {out['price_pp']:,} 起）"}
    return {"status": "ok", "price_pp": back["price_pp"]}


def check_agoda(page, deal: dict) -> dict:
    bodies: list[str] = []
    page.on("response", lambda r: _keep(r, "flights-bff/search/v1/flights", bodies))
    page.goto(search_url("agoda", deal), wait_until="domcontentloaded", timeout=60000)
    _wait(page, lambda: bodies and agoda_options(bodies[-1])[1], 50, "Agoda 去程清單")
    if not agoda_options(bodies[-1])[0] and '"items"' in bodies[-1]:
        raise SiteError("Agoda 顯示的不是台幣價格")
    out = find(agoda_options(bodies[-1])[0], deal["out"])
    if not out:
        return {"status": "not_found", "note": f"Agoda 沒有 {deal['out']['flight']}"}
    n = len(bodies)
    _card(page, out).click(timeout=15000)       # expands the card
    page.wait_for_timeout(1500)
    page.get_by_role("button", name="選擇航班").first.click(timeout=15000)

    def returns():
        new = [agoda_options(b) for b in bodies[n:]]
        return next((o for o, done, sel in reversed(new) if sel and done), None)
    back = find(_wait(page, returns, 50, "Agoda 回程清單"), deal["back"])
    if not back:
        return {"status": "not_found", "note": f"Agoda 沒有 {deal['back']['flight']}（去程 {out['price_pp']:,} 起）"}
    return {"status": "ok", "price_pp": back["price_pp"]}


CHECKERS = {"trip": check_trip, "agoda": check_agoda}


def sane(r: dict, google_pp: int) -> dict:
    """Same flights cannot cost a fraction (or a multiple) of Google's price: treat that as a parsing error."""
    if r.get("status") == "ok" and not SANE_RATIO[0] <= r["price_pp"] / google_pp <= SANE_RATIO[1]:
        return {"status": "error", "note": f"價格異常（{r['price_pp']:,}，Google {google_pp:,}），可能幣別不對"}
    return r


def check_deal(pw, deal: dict, sites=tuple(SITES)) -> dict:
    """{site: {status ok|not_found|error, price_pp?, note?, url, at}} for one deal; never raises."""
    res = {}
    for site in sites:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="zh-TW", timezone_id="Asia/Taipei",
                                  viewport={"width": 1366, "height": 900})
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.new_page()
        try:
            r = CHECKERS[site](page, deal)
        except Exception as e:  # noqa: BLE001 - site changed / blocked / slow: report, never crash the run
            r = {"status": "error", "note": f"{type(e).__name__}: {str(e)[:160]}"}
        finally:
            browser.close()
        r = sane(r, deal["price_pp"])
        r.update(url=search_url(site, deal), at=now_tpe().isoformat(timespec="minutes"))
        res[site] = r
    return res


# ------------------------------------------------------------------ selection / bookkeeping (pure)
def pick_targets(deals: list[dict], cfg: Config, today: date, limit: int = 5) -> list[dict]:
    muted = set(cfg.muted)
    start = cfg.date_range(today)[0].isoformat()
    ok = [d for d in deals if d["key"] not in muted and d["dep"] >= start and d.get("out") and d.get("back")]
    return sorted(ok, key=lambda d: (d["price_pp"], d["dep"]))[:limit]


def summarize(result: dict, google_pp: int) -> dict | None:
    """Cheapest successful OTA price for a deal: {site, name, price_pp, url, saving}."""
    best = None
    for site, r in result.items():
        if site in SITES and r.get("status") == "ok" and (best is None or r["price_pp"] < best["price_pp"]):
            best = {"site": site, "name": SITES[site], "price_pp": r["price_pp"], "url": r["url"],
                    "saving": google_pp - r["price_pp"]}
    return best


def fresh(entry: dict | None, now: datetime) -> bool:
    try:
        return bool(entry) and (now - datetime.fromisoformat(entry["at"])).total_seconds() < RESULT_TTL_HOURS * 3600
    except (KeyError, ValueError):
        return False


def attach(deals: list[dict], ota_data: dict, now: datetime) -> None:
    """Add `ota` (per-site results + cheapest) to deals that were checked recently."""
    checked = ota_data.get("checked") or {}
    for d in deals:
        e = checked.get(d["key"])
        if fresh(e, now):
            d["ota"] = {"sites": {s: e[s] for s in SITES if s in e}, "best": summarize(e, d["price_pp"]),
                        "at": e["at"]}


def to_notify(targets: list[dict], ota_data: dict, cfg: Config) -> list[dict]:
    """Deals where an OTA is clearly cheaper than Google and within the digest price range; each
    deal/site/price is reported once (again only if it drops by `renotify_drop`)."""
    sent = ota_data.setdefault("notified", {})
    out = []
    for d in targets:
        best = summarize((ota_data.get("checked") or {}).get(d["key"], {}), d["price_pp"])
        if not best or best["saving"] < CHEAPER_BY or best["price_pp"] > cfg.digest_max:
            continue
        k = f"{d['key']}|{best['site']}"
        if k in sent and best["price_pp"] > sent[k] - cfg.renotify_drop:
            continue
        sent[k] = best["price_pp"]
        out.append({**d, "ota": {"best": best}})
    return out


# ------------------------------------------------------------------ run
def run(cfg: Config, dry_run: bool = False, limit: int = 5, now: datetime | None = None) -> int:
    from playwright.sync_api import sync_playwright

    from . import emails
    from .app import _send

    now = now or now_tpe()
    latest = store.load("latest.json", {})
    data = store.load("ota.json", {})
    if data.get("version") != DATA_VERSION:  # drop results / "already notified" prices from older versions
        data = {"version": DATA_VERSION}
    state = store.load_state()
    targets = pick_targets(latest.get("deals") or [], cfg, now.date(), limit)
    print(f"[OTA] 核對 {len(targets)} 個 Google 最便宜的行程")
    checked = data.setdefault("checked", {})
    with sync_playwright() as pw:
        for d in targets:
            r = check_deal(pw, d)
            checked[d["key"]] = {**r, "google_pp": d["price_pp"], "at": now.isoformat(timespec="minutes")}
            line = "｜".join(f"{SITES[s]} " + (f"{v['price_pp']:,}" if v["status"] == "ok" else
                                               ("沒有同航班" if v["status"] == "not_found" else "查詢失敗"))
                            for s, v in r.items())
            print(f"   {d['key']} Google {d['price_pp']:,}｜{line}")
            for s, v in r.items():
                if v["status"] == "error":
                    print(f"      [{SITES[s]}] {v.get('note')}")
    for k in [k for k, v in checked.items() if k.split("|")[1] < now.date().isoformat() or not fresh(v, now)]:
        del checked[k]
    errors = sum(1 for d in targets for s in SITES if checked[d["key"]][s]["status"] == "error")
    data["last_run"] = {"at": now.isoformat(timespec="minutes"), "targets": len(targets), "errors": errors}
    data.setdefault("log", []).append(data["last_run"])
    data["log"] = data["log"][-30:]

    paused = cfg.is_paused(now.date())
    cheaper = to_notify(targets, data, cfg)
    if cheaper and not paused:
        subject, text, html = emails.ota_cheaper(cheaper, cfg)
        if not _send(state, subject, text, html, dry_run):
            for d in cheaper:
                data["notified"].pop(f"{d['key']}|{d['ota']['best']['site']}", None)
        store.save("state.json", state)
    store.save("ota.json", data)
    if latest.get("deals"):
        attach(latest["deals"], data, now)
        store.save("latest.json", latest)
    lower = sum(1 for d in targets if (summarize(checked.get(d["key"], {}), d["price_pp"]) or {}).get("saving", 0) > 0)
    print(f"[OTA] 完成；{lower} 個行程 OTA 比 Google 便宜，其中 {len(cheaper)} 個寄通知"
          f"（便宜 {CHEAPER_BY} 以上且每人 ≤ {cfg.digest_max:,}）；查詢失敗 {errors} 次")
    return 0
