"""Email content: instant alerts, daily digest, health notices (plain text + HTML)."""

from __future__ import annotations

from datetime import date, datetime
from html import escape

from .airports import WEEKDAYS, airline_name, airline_site, airport_name
from .config import Config, fmt_hhmm

CONSOLE_URL = "https://ntcloudy.github.io/jp-flight-alert/"


def money(n: float | int | None) -> str:
    return "—" if n is None else f"NT${round(n):,}"


def short_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def route_title(deal: dict) -> str:
    return f"{airport_name(deal['dest'])}（{airport_name(deal['origin'])}⇄{deal['dest']}）"


def _airlines(deal: dict) -> str:
    a, b = deal["out"]["airline"], deal["back"]["airline"]
    return airline_name(a) if a == b else f"{airline_name(a)}／{airline_name(b)}"


def flights_line(deal: dict) -> str:
    o, b = deal["out"], deal["back"]
    return f"去 {o['flight']} {o['dep']}→{o['arr']}｜回 {b['flight']} {b['dep']}→{b['arr']}"


def notes(deal: dict) -> list[str]:
    out = [f"⚠️ {f}" for f in deal.get("flags") or []]
    if deal.get("discount_pct") is not None and deal["discount_pct"] >= 10:
        out.append(f"📉 比這條航線平常便宜 {deal['discount_pct']}%")
    return out


def how_to_find(deal: dict) -> str:
    return (f"在 Google Flights 先選去程 {deal['out']['flight']}（{deal['out']['dep']}），再選回程 "
            f"{deal['back']['flight']}（{deal['back']['dep']}），會看到 {deal['adults']} 人總價 {money(deal['price_total'])}")


def deal_text(deal: dict, icon: str = "✈️") -> str:
    lines = [
        f"{icon} {route_title(deal)} 每人 {money(deal['price_pp'])}（{deal['adults']} 人共 {money(deal['price_total'])}）",
        f"   {short_date(deal['dep'])} → {short_date(deal['ret'])}・{deal['days']} 天・{_airlines(deal)}",
        f"   {flights_line(deal)}",
    ]
    lines += [f"   {n}" for n in notes(deal)]
    lines.append(f"   👉 {deal['url']}")
    lines.append(f"   （{how_to_find(deal)}）")
    return "\n".join(lines)


# ---- grouping: same route + same flights, different dates -> one entry ----------------------
def group_key(deal: dict) -> tuple:
    o, b = deal["out"], deal["back"]
    return (deal["route"], o["flight"], o["dep"], b["flight"], b["dep"])


def group_deals(deals: list[dict]) -> list[list[dict]]:
    """Merge itineraries that differ only by date. Keeps input order (cheapest first if input is sorted)."""
    groups: dict[tuple, list[dict]] = {}
    for d in deals:
        groups.setdefault(group_key(d), []).append(d)
    out = []
    for items in groups.values():
        out.append(sorted(items, key=lambda d: (d["price_pp"], d["dep"])))
    return sorted(out, key=lambda g: g[0]["price_pp"])


def common_flags(items: list[dict]) -> list[str]:
    sets = [set(d.get("flags") or []) for d in items]
    common = set.intersection(*sets) if sets else set()
    return [f for f in (items[0].get("flags") or []) if f in common]


def date_line(d: dict, skip: list[str] = ()) -> str:
    extra = "".join(f" ⚠️{f}" for f in d.get("flags") or [] if f not in skip)
    return f"{short_date(d['dep'])} → {short_date(d['ret'])}・{d['days']} 天｜每人 {money(d['price_pp'])}{extra}"


def group_text(items: list[dict], icon: str = "✈️") -> str:
    if len(items) == 1:
        return deal_text(items[0], icon)
    best = items[0]
    common = common_flags(items)
    lines = [f"{icon} {route_title(best)} 每人 {money(best['price_pp'])} 起・{len(items)} 個日期・{_airlines(best)}",
             f"   {flights_line(best)}（每個日期都是這組航班）"]
    lines += [f"   ⚠️ {f}" for f in common]
    if len({d.get("discount_pct") for d in items}) == 1 and (best.get("discount_pct") or 0) >= 10:
        lines.append(f"   📉 每個日期都比這條航線平常便宜 {best['discount_pct']}%")
    for d in sorted(items, key=lambda d: d["dep"]):
        lines.append(f"   • {date_line(d, common)}")
        lines.append(f"     👉 {d['url']}")
    return "\n".join(lines)


# ---- HTML helpers --------------------------------------------------------------------------
STYLE_BTN = ("display:inline-block;padding:7px 14px;border-radius:8px;background:#0b63ce;color:#fff;"
             "text-decoration:none;font-weight:600;font-size:14px")


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><body style=\"margin:0;background:#f4f6fa;font-family:-apple-system,'Segoe UI',"
        "'Noto Sans TC','PingFang TC','Microsoft JhengHei',sans-serif;color:#1d2433\">"
        "<div style=\"max-width:640px;margin:0 auto;padding:16px\">"
        f"<div style=\"background:linear-gradient(135deg,#0b63ce,#36a3f7);color:#fff;border-radius:14px;padding:16px 18px\">"
        f"<div style=\"font-size:20px;font-weight:700\">{escape(title)}</div></div>"
        f"{body}"
        f"<p style=\"color:#6b7385;font-size:12px;text-align:center;margin-top:18px\">"
        f"價格為 Google Flights 查到的含稅總價換算每人，實際以航空公司結帳畫面為準｜本工具只通知、不會自動訂票<br>"
        f"<a href=\"{CONSOLE_URL}\" style=\"color:#0b63ce\">網頁控制台</a>：修改條件、暫停通知</p>"
        "</div></body></html>"
    )


def _section(title: str, inner: str) -> str:
    return (f"<div style=\"background:#fff;border:1px solid #e3e7ef;border-radius:14px;padding:14px 16px;margin-top:14px\">"
            f"<div style=\"font-size:16px;font-weight:700;margin-bottom:8px\">{escape(title)}</div>{inner}</div>")


def deal_html(deal: dict, highlight: bool = False) -> str:
    site = airline_site(deal["out"]["airline"])
    note_html = "".join(f"<div style=\"color:#b45309;font-size:13px\">{escape(n)}</div>" for n in notes(deal))
    border = "#f59e0b" if highlight else "#e3e7ef"
    site_link = (f" <a href=\"{escape(site)}\" style=\"color:#0b63ce;font-size:13px;margin-left:8px\">"
                 f"{escape(airline_name(deal['out']['airline']))}官網</a>") if site else ""
    return (
        f"<div style=\"border:1px solid {border};border-radius:12px;padding:10px 12px;margin:8px 0\">"
        f"<div style=\"font-size:15px;font-weight:700\">{escape(route_title(deal))}"
        f"<span style=\"float:right;color:#c2410c\">每人 {money(deal['price_pp'])}</span></div>"
        f"<div style=\"font-size:14px;margin-top:2px\">{escape(short_date(deal['dep']))} → {escape(short_date(deal['ret']))}"
        f"・{deal['days']} 天・{escape(_airlines(deal))}"
        f"<span style=\"float:right;color:#6b7385;font-size:13px\">{deal['adults']} 人共 {money(deal['price_total'])}</span></div>"
        f"<div style=\"font-size:14px;color:#374151;margin-top:2px\">{escape(flights_line(deal))}</div>{note_html}"
        f"<div style=\"font-size:12px;color:#6b7385;margin-top:6px\">{escape(how_to_find(deal))}</div>"
        f"<div style=\"margin-top:8px\"><a href=\"{escape(deal['url'])}\" style=\"{STYLE_BTN}\">查看航班／訂票</a>{site_link}</div>"
        "</div>"
    )


def group_html(items: list[dict], highlight: bool = False) -> str:
    if len(items) == 1:
        return deal_html(items[0], highlight)
    best = items[0]
    common = common_flags(items)
    border = "#f59e0b" if highlight else "#e3e7ef"
    rows = "".join(
        f"<tr><td style=\"padding:6px 4px;border-bottom:1px solid #eef1f6\">{escape(short_date(d['dep']))} → "
        f"{escape(short_date(d['ret']))}・{d['days']} 天"
        + "".join(f"<div style=\"color:#b45309;font-size:12px\">⚠️ {escape(f)}</div>" for f in d.get("flags") or []
                  if f not in common)
        + f"</td><td style=\"padding:6px 4px;border-bottom:1px solid #eef1f6;text-align:right;white-space:nowrap;"
        f"color:#c2410c;font-weight:700\">{money(d['price_pp'])}</td>"
        f"<td style=\"padding:6px 4px;border-bottom:1px solid #eef1f6;text-align:right\">"
        f"<a href=\"{escape(d['url'])}\" style=\"color:#0b63ce;font-size:13px;white-space:nowrap\">查看／訂票</a></td></tr>"
        for d in sorted(items, key=lambda d: d["dep"]))
    return (
        f"<div style=\"border:1px solid {border};border-radius:12px;padding:10px 12px;margin:8px 0\">"
        f"<div style=\"font-size:15px;font-weight:700\">{escape(route_title(best))}"
        f"<span style=\"float:right;color:#c2410c\">每人 {money(best['price_pp'])} 起</span></div>"
        f"<div style=\"font-size:14px;margin-top:2px\">{len(items)} 個日期都有特價・{escape(_airlines(best))}</div>"
        f"<div style=\"font-size:14px;color:#374151;margin-top:2px\">{escape(flights_line(best))}（每個日期都是這組航班）</div>"
        + "".join(f"<div style=\"color:#b45309;font-size:13px\">⚠️ {escape(f)}</div>" for f in common)
        + f"<table style=\"width:100%;border-collapse:collapse;font-size:14px;margin-top:6px\">{rows}</table>"
        "</div>"
    )


# ---- instant alert -------------------------------------------------------------------------
def instant(deals: list[dict], cfg: Config) -> tuple[str, str, str]:
    groups = group_deals(deals)
    best = groups[0][0]
    if len(deals) == 1:
        subject = (f"🔥 超便宜機票：{airport_name(best['dest'])} 每人 {money(best['price_pp'])}"
                   f"（{_airlines(best)}）{short_date(best['dep'])}–{short_date(best['ret'])}")
    elif len(groups) == 1:
        subject = (f"🔥 超便宜機票：{airport_name(best['dest'])} 每人 {money(best['price_pp'])} 起"
                   f"（{_airlines(best)}，{len(deals)} 個日期）")
    else:
        subject = (f"🔥 {len(groups)} 組超便宜機票（共 {len(deals)} 個日期）！最低 {airport_name(best['dest'])} "
                   f"每人 {money(best['price_pp'])}")
    intro = f"找到每人 {money(cfg.instant_max)} 以內、符合你時段的傳統航空直飛來回機票（{cfg.adults} 人經濟艙）："
    text = intro + "\n\n" + "\n\n".join(group_text(g, "🔥") for g in groups)
    text += "\n\n便宜機票常常很快賣完，建議盡快確認。\n網頁控制台：" + CONSOLE_URL
    html = _page("🔥 超便宜機票", _section(intro, "".join(group_html(g, True) for g in groups)))
    return subject, text, html


# ---- daily digest --------------------------------------------------------------------------
def _pick(deals: list[dict], per_dest: int, limit: int) -> list[list[dict]]:
    """Group same-flight itineraries first, then cap groups per destination and in total."""
    out, count = [], {}
    for g in group_deals(deals):
        dest = g[0]["dest"]
        if count.get(dest, 0) >= per_dest:
            continue
        count[dest] = count.get(dest, 0) + 1
        out.append(g)
        if len(out) >= limit:
            break
    return out


def digest(cfg: Config, latest: dict, promos: list[dict], now: datetime) -> tuple[str, str, str]:
    deals = [d for d in latest.get("deals", []) if d.get("key") not in set(cfg.muted)]
    hot = _pick([d for d in deals if d.get("category") == "instant"], 3, 10)
    good = _pick([d for d in deals if d.get("category") == "digest"], 3, 12)
    odd = _pick([d for d in deals if d.get("category") == "anomaly"], 2, 6)

    parts = []
    if hot:
        parts.append(f"超便宜 {len(hot)} 組")
    if good:
        parts.append(f"{cfg.digest_max // 10000 if cfg.digest_max % 10000 == 0 else cfg.digest_max:,}"
                     f"{'萬' if cfg.digest_max % 10000 == 0 else ''}內 {len(good)} 組")
    if odd:
        parts.append(f"特別便宜 {len(odd)} 組")
    if promos:
        parts.append(f"促銷 {len(promos)} 則")
    subject = f"✈️ 每日機票整理 {now.month}/{now.day}：" + ("・".join(parts) if parts else "今天沒有符合門檻的機票")

    text_blocks, html_blocks = [], []

    def add(title: str, groups: list[list[dict]], icon: str, highlight: bool = False):
        text_blocks.append(f"【{title}】\n" + "\n\n".join(group_text(g, icon) for g in groups))
        html_blocks.append(_section(title, "".join(group_html(g, highlight) for g in groups)))

    if hot:
        add(f"🔥 超便宜（每人 {money(cfg.instant_max)} 以內）", hot, "🔥", True)
    if good:
        add(f"👍 划算（每人 {money(cfg.digest_max)} 以內）", good, "👍")
    if odd:
        add(f"📉 特別便宜（比平常便宜 {cfg.anomaly_pct}% 以上）", odd, "📉")
    if not (hot or good or odd):
        msg = f"目前沒有每人 {money(cfg.digest_max)} 以內、或比平常便宜 {cfg.anomaly_pct}% 以上的機票。"
        text_blocks.append(msg)
        html_blocks.append(_section("😴 今天沒有特別便宜的", f"<div style=\"font-size:14px\">{escape(msg)}</div>"))

    if promos:
        t = "\n".join(f"- {p['title']}（{p.get('source') or '新聞'}）\n  {p['link']}" for p in promos)
        text_blocks.append("【📣 航空公司促銷消息】\n" + t)
        h = "".join(
            f"<div style=\"margin:6px 0;font-size:14px\">• <a href=\"{escape(p['link'])}\" style=\"color:#0b63ce\">"
            f"{escape(p['title'])}</a> <span style=\"color:#6b7385;font-size:12px\">{escape(p.get('source') or '')}</span></div>"
            for p in promos)
        html_blocks.append(_section("📣 航空公司促銷消息", h))

    # destination table: the verified cheapest itinerary per route, else the calendar reference
    verified_best: dict[str, dict] = {}
    for d in deals:
        if d["route"] not in verified_best:
            verified_best[d["route"]] = d
    rows = []
    for route, info in latest.get("routes", {}).items():
        if info.get("no_service") or info.get("min_pp") is None:
            continue
        v = verified_best.get(route)
        rows.append((v["price_pp"] if v else info["min_pp"], route, info, v))
    rows.sort(key=lambda r: r[0])
    if rows:
        lines, trs = [], []
        for price, route, info, v in rows[:20]:
            name = f"{airport_name(info['dest'])}（{airport_name(info['origin'])}）"
            if v:
                when = f"{short_date(v['dep'])}・{v['days']} 天・{v['out']['flight']}/{v['back']['flight']}"
            elif info.get("near_miss"):
                when = f"參考價，還沒有完全符合時段的航班（{info['near_miss']['why']}）"
            else:
                when = f"{short_date(info['min_dep'])} 出發（參考價，航班確認中）"
            lines.append(f"- {name}：每人 {money(price)}｜{when}")
            trs.append(f"<tr><td style=\"padding:5px 4px;border-bottom:1px solid #eef1f6\">{escape(name)}</td>"
                       f"<td style=\"padding:5px 4px;border-bottom:1px solid #eef1f6;text-align:right;white-space:nowrap\">"
                       f"{money(price)}</td><td style=\"padding:5px 4px;border-bottom:1px solid #eef1f6;color:#6b7385;"
                       f"font-size:13px\">{escape(when)}</td></tr>")
        text_blocks.append("【📊 各目的地目前最低價（每人）】\n" + "\n".join(lines))
        html_blocks.append(_section("📊 各目的地目前最低價（每人）",
                                    f"<table style=\"width:100%;border-collapse:collapse;font-size:14px\">{''.join(trs)}</table>"))

    st = latest.get("stats") or {}
    status = (f"過去 24 小時查詢 {st.get('requests_24h', 0)} 次、成功率 {st.get('success_rate_24h', '—')}；"
              f"資料完整度 {round((latest.get('coverage') or 0) * 100)}%")
    cond = (f"條件：{cfg.adults} 人經濟艙・直飛・玩 {cfg.min_days}–{cfg.max_days} 天｜"
            f"去程 {fmt_hhmm(cfg.outbound.depart_earliest)}–{fmt_hhmm(cfg.outbound.depart_latest)} 起飛、"
            f"{fmt_hhmm(cfg.outbound.arrive_latest)} 前抵達｜回程 {fmt_hhmm(cfg.inbound.depart_earliest)}–"
            f"{fmt_hhmm(cfg.inbound.depart_latest)} 起飛、{fmt_hhmm(cfg.inbound.arrive_latest)} 前抵達")
    text = "\n\n".join(text_blocks) + f"\n\n{cond}\n{status}\n網頁控制台：{CONSOLE_URL}"
    html_blocks.append(f"<p style=\"color:#6b7385;font-size:12px;margin-top:12px\">{escape(cond)}<br>{escape(status)}</p>")
    html = _page(f"✈️ 每日機票整理 {now.month}/{now.day}", "".join(html_blocks))
    return subject, text, html


# ---- health ------------------------------------------------------------------------------
def health(problem: str, detail: str) -> tuple[str, str]:
    subject = f"⚠️ 日本機票提醒：{problem}"
    text = (f"{problem}\n\n{detail}\n\n系統會自動重試，恢復後不需要做任何事。如果持續好幾天都這樣，"
            f"可能是 Google 改版或擋住了 GitHub 的查詢，請告訴維護者。\n網頁控制台：{CONSOLE_URL}")
    return subject, text
