"""Temporary watcher: email me about Cathay Pacific typhoon / Taiwan / Japan travel advisories.

Every run reads Cathay's "Latest news -> Flight disruptions" lists (TW / HK / JP sites). For each relevant
advisory (typhoon, Taipei, Japan, weather ...) it emails the content the first time it is seen and again
whenever its "Latest update" timestamp changes. Stops after WATCH_UNTIL. State: file given as argv[1].

python scripts/cx_watch.py .cx-watch/state.json
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flight_alert.notifier import send_email  # noqa: E402

TPE = timezone(timedelta(hours=8))
WATCH_UNTIL = datetime(2026, 10, 24, 12, 0, tzinfo=TPE)
FLIGHT = "CX564 10/23 桃園 → 大阪關西"
BASE = "https://www.cathaypacific.com"
LISTS = [f"{BASE}/cx/{loc}/latest-news.html" for loc in ("zh_TW", "en_TW", "zh_HK", "en_HK", "en_JP")]
RELEVANT = re.compile(r"typhoon|颱風|台風|champi|薔琵|taipei|taiwan|台北|台灣|臺灣|osaka|kansai|大阪|關西|"
                      r"japan|日本|weather|天氣|惡劣", re.I)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "ignore")


def text_of(page: str) -> str:
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).replace("\x00", "").strip()


def advisories() -> dict[str, tuple[str, str]]:
    """{slug: (URL, title)} from every listing page, preferring the Traditional Chinese (Taiwan) version."""
    found: dict[str, tuple[str, str]] = {}
    for url in LISTS:
        try:
            page = fetch(url)
        except Exception as e:
            print(f"[skip] {url}: {e}")
            continue
        for m in re.finditer(r'<a[^>]+href="([^"]*latest-news/flight-disruptions/[^"?#]+\.html)[^"]*"[^>]*>(.*?)</a>',
                             page, flags=re.S | re.I):
            href, label = m.group(1), text_of(m.group(2))
            full = href if href.startswith("http") else BASE + href
            slug = full.rsplit("/", 1)[-1]
            if slug not in found or "/zh_TW/" in full:
                found[slug] = (full, label or slug)
    return found


def summary(body: str) -> tuple[str, str]:
    """(last-update stamp, readable advisory text) from an advisory page."""
    m = re.search(r"(最後更新|Latest update)\s*[:：]\s*(.{6,40}?\(GMT\+8\))", body)
    stamp = m.group(2).strip() if m else ""
    start = body.find(m.group(0)) if m else 0
    end_marks = [i for i in (body.find("聯絡客戶服務團隊", start), body.find("Contact Customer Care", start)) if i > 0]
    text = body[start:min(end_marks) if end_marks else start + 1800]
    text = re.sub(r"\s*(往返／途經航班|Flights to / from / through)", r"\n\n\1", text)
    text = re.sub(r"\s*(有關重新預訂機票的更多資訊|Please note the following|如何更改你的預訂|How to make changes)", r"\n\n\1", text)
    return stamp, text.strip()


def main() -> int:
    now = datetime.now(TPE)
    if now > WATCH_UNTIL:
        print("watch window over")
        return 0
    state_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".cx-watch/state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    updates: dict[str, str] = state.get("updates", {})
    current = advisories()
    if not current:
        print("no listing could be read (site blocked?)")
        return 0
    print("listed: " + ", ".join(current))
    sent = 0
    for slug, (url, title) in current.items():
        try:
            body = text_of(fetch(url))
        except Exception as e:
            print(f"[warn] cannot open {url}: {e}")
            continue
        stamp, text = summary(body)
        if not RELEVANT.search(title + " " + text):  # judge the advisory itself, not the site's menus
            continue
        key = stamp or text[:300]
        if updates.get(slug) == key:
            continue
        again = slug in updates
        updates[slug] = key
        send_email(f"【國泰{'公告更新' if again else '公告'}】{title}" + (f"（{stamp}）" if stamp else ""),
                   f"國泰航空{'更新了' if again else '發布了'}可能影響你航班（{FLIGHT}）的公告：\n{url}\n\n{text}\n\n"
                   "—\n如果要改期：國泰官網或 App 的「管理預訂」可以自己改；旅行社開的票要找原旅行社。\n"
                   "這封信由自動監控寄出，國泰之後更新這則公告或發布新公告時會再通知你（監控到 10/24 中午）。")
        sent += 1
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"updates": updates, "checked": now.isoformat()}, ensure_ascii=False, indent=1))
    print(f"emails sent: {sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
