"""Airport / airline metadata (Traditional Chinese names, airline websites)."""

from __future__ import annotations

TAIWAN_AIRPORTS = {
    "TPE": "桃園",
    "TSA": "松山",
    "RMQ": "台中",
    "KHH": "高雄",
}

JAPAN_AIRPORTS = {
    "NRT": "東京成田",
    "HND": "東京羽田",
    "KIX": "大阪關西",
    "UKB": "神戶",
    "NGO": "名古屋",
    "FUK": "福岡",
    "KKJ": "北九州",
    "CTS": "札幌",
    "HKD": "函館",
    "AKJ": "旭川",
    "OKA": "沖繩",
    "ISG": "石垣島",
    "SHI": "宮古島",
    "SDJ": "仙台",
    "AOJ": "青森",
    "AXT": "秋田",
    "HNA": "花卷",
    "GAJ": "山形",
    "FKS": "福島",
    "KIJ": "新潟",
    "IBR": "茨城",
    "FSZ": "靜岡",
    "TOY": "富山",
    "KMQ": "小松",
    "HIJ": "廣島",
    "OKJ": "岡山",
    "YGJ": "米子",
    "IZO": "出雲",
    "TAK": "高松",
    "MYJ": "松山（愛媛）",
    "TKS": "德島",
    "KCZ": "高知",
    "KMJ": "熊本",
    "KOJ": "鹿兒島",
    "KMI": "宮崎",
    "OIT": "大分",
    "NGS": "長崎",
    "HSG": "佐賀",
}

# Songshan (TSA) only has Japan service to Haneda; scanning other pairs wastes requests.
SONGSHAN_DESTINATIONS = {"HND"}

FULL_SERVICE_AIRLINES = {
    "CI": ("中華航空", "https://www.china-airlines.com/tw/zh"),
    "BR": ("長榮航空", "https://www.evaair.com/zh-tw/index.html"),
    "JX": ("星宇航空", "https://www.starlux-airlines.com/zh-TW"),
    "AE": ("華信航空", "https://www.mandarin-airlines.com/"),
    "JL": ("日本航空", "https://www.jal.co.jp/tw/zhtw/"),
    "NH": ("全日空", "https://www.ana.co.jp/zh/tw/"),
    "CX": ("國泰航空", "https://www.cathaypacific.com/cx/zh_TW.html"),
    "7G": ("星悅航空", "https://www.starflyer.jp/tw/"),
    "TG": ("泰國航空", "https://www.thaiairways.com/zh-tw/"),
}

# Low-cost carriers seen on Taiwan <-> Japan routes (never whitelisted; used for display only).
LOW_COST_AIRLINES = {
    "IT": "台灣虎航", "MM": "樂桃航空", "GK": "捷星日本", "TR": "酷航", "ZG": "ZIPAIR",
    "IJ": "春秋航空日本", "UO": "香港快運", "D7": "亞洲航空 X", "VJ": "越捷航空",
    "SL": "泰國獅子航空", "FD": "泰國亞洲航空", "7C": "濟州航空", "BX": "釜山航空",
}

WEEKDAYS = "一二三四五六日"


def airport_name(code: str) -> str:
    return JAPAN_AIRPORTS.get(code) or TAIWAN_AIRPORTS.get(code) or code


def airline_name(code: str) -> str:
    if code in FULL_SERVICE_AIRLINES:
        return FULL_SERVICE_AIRLINES[code][0]
    return LOW_COST_AIRLINES.get(code, code)


def airline_site(code: str) -> str | None:
    entry = FULL_SERVICE_AIRLINES.get(code)
    return entry[1] if entry else None
