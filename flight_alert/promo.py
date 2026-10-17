"""Airline promotion monitor: Google News RSS search for full-service airline sales to Japan.

Airline websites block automated requests, so we watch news coverage instead: Taiwanese media
report almost every China Airlines / EVA / STARLUX / JAL / ANA / Cathay sale within hours.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from .config import TPE_TZ

QUERIES = [
    "(華航 OR 中華航空 OR 長榮航空 OR 星宇 OR 日航 OR 日本航空 OR 全日空 OR 國泰航空) "
    "(日本 OR 東京 OR 大阪 OR 沖繩 OR 北海道 OR 札幌 OR 福岡 OR 名古屋 OR 日韓 OR 全航線) "
    "(促銷 OR 特價 OR 優惠 OR 早鳥 OR 開賣 OR 旅展 OR 折扣 OR 快閃) when:3d",
]

AIRLINES = {
    "CI": r"華航|中華航空",
    "BR": r"長榮航空|長榮(?=.{0,12}(機票|航線|飛|促銷|優惠|早鳥|旅展))",
    "JX": r"星宇",
    "JL": r"日航|日本航空|\bJAL\b",
    "NH": r"全日空|\bANA\b",
    "CX": r"國泰航空|國泰(?!世華)(?=.{0,12}(機票|航線|飛|促銷|優惠|旅展))",
}
PROMO_RE = re.compile(r"促銷|特價|優惠|早鳥|開賣|開搶|搶購|限時|快閃|旅展|降價|特惠|最低|有找|元起|[0-9一二三四五六七八九]+折|買一送一|買1送1")
JAPAN_RE = re.compile(r"日本|東京|大阪|京都|關西|沖繩|那霸|北海道|札幌|函館|福岡|九州|名古屋|仙台|東北|熊本|鹿兒島|"
                      r"廣島|岡山|高松|四國|富山|小松|北陸|靜岡|青森|秋田|花卷|新潟|石垣|宮古|日韓|日線|"
                      r"全航線|全球航線|全球.{0,6}航點|亞洲線|亞洲航線|東北亞")
EXCLUDE_RE = re.compile(r"懶人包.{0,20}卡|信用卡.{0,20}(回饋|比較|懶人包|推薦)|(優惠碼|折扣碼)》|Klook|KKday|Trip\.com|"
                        r"國泰世華|Buffet|吃到飽|機加酒|住這裡|房價|股價|股東|營收|財報|罷工|停飛|事故|延誤|徵才|招募|空服員")
DISCOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9])?折|買[一1]送[一1]|[0-9,]+元")


def _clean_title(title: str) -> tuple[str, str]:
    """'Headline - Source' -> ('Headline', 'Source')."""
    title = (title or "").strip()
    if " - " in title:
        head, src = title.rsplit(" - ", 1)
        return head.strip(), src.strip()
    return title, ""


def match(title: str) -> list[str] | None:
    """Airline codes if the headline looks like a full-service airline sale relevant to Japan."""
    if EXCLUDE_RE.search(title) or not PROMO_RE.search(title) or not JAPAN_RE.search(title):
        return None
    codes = [code for code, pat in AIRLINES.items() if re.search(pat, title)]
    return codes or None


def _norm(title: str) -> str:
    return re.sub(r"[\W_]+", "", title).lower()


def _similar(a: str, b: str) -> bool:
    grams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}  # noqa: E731
    ga, gb = grams(_norm(a)), grams(_norm(b))
    return bool(ga and gb) and len(ga & gb) / len(ga | gb) >= 0.55


def parse_rss(xml_text: str) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title, source = _clean_title(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        if src_el is not None and (src_el.text or "").strip():
            source = src_el.text.strip()
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            published = None
        items.append({"title": title, "source": source, "link": link,
                      "published": published.isoformat(timespec="minutes") if published else None})
    return items


def fetch(session: requests.Session | None = None, timeout: int = 20) -> list[dict]:
    s = session or requests.Session()
    out: list[dict] = []
    for q in QUERIES:
        url = f"https://news.google.com/rss/search?q={quote(q)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        r = s.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (jp-flight-alert)"})
        r.raise_for_status()
        out.extend(parse_rss(r.text))
    return out


def _signatures(title: str, codes: list[str]) -> set:
    """(airline, headline discount) pairs, e.g. {('JX', '85折')}: the same sale reported by many outlets."""
    m = DISCOUNT_RE.search(title)
    return {(c, m.group(0)) for c in codes} if m else set()


def select(items: list[dict], seen: dict, now: datetime, recent: list[dict] | None = None,
           max_age_days: int = 4, per_airline: int = 2) -> list[dict]:
    """New, relevant, de-duplicated promo headlines (newest first).

    `recent` holds headlines already reported in the last two weeks; the same sale covered by
    another outlet later is treated as a duplicate."""
    cutoff = now - timedelta(days=max_age_days)
    picked: list[dict] = []
    earlier = list(recent or [])
    signatures: set = set()
    for p in earlier:
        signatures |= _signatures(p["title"], p.get("airlines") or [])
    per: dict[str, int] = {}
    for it in sorted(items, key=lambda x: x.get("published") or "", reverse=True):
        codes = match(it["title"])
        if not codes:
            continue
        if it.get("published") and datetime.fromisoformat(it["published"]) < cutoff:
            continue
        pid = hashlib.sha1(_norm(it["title"]).encode()).hexdigest()[:12]
        sigs = _signatures(it["title"], codes)
        if pid in seen or (sigs and sigs <= signatures) or any(_similar(it["title"], p["title"]) for p in picked + earlier):
            continue
        if all(per.get(c, 0) >= per_airline for c in codes):
            continue
        for c in codes:
            per[c] = per.get(c, 0) + 1
        signatures |= sigs
        picked.append({**it, "id": pid, "airlines": codes,
                       "found_at": now.astimezone(TPE_TZ).isoformat(timespec="minutes")})
    return picked


def update_state(state: dict, new_items: list[dict], now: datetime, keep_days: int = 14) -> None:
    today = now.astimezone(TPE_TZ).date()
    for it in new_items:
        state["promo_seen"][it["id"]] = today.isoformat()
    state["promo_pending"] = (state.get("promo_pending") or []) + new_items
    recent = new_items + (state.get("promo_recent") or [])
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    state["promo_recent"] = [p for p in recent if (p.get("found_at") or "")[:10] >= cutoff][:40]
    seen_cutoff = (today - timedelta(days=45)).isoformat()
    state["promo_seen"] = {k: v for k, v in state["promo_seen"].items() if v >= seen_cutoff}
