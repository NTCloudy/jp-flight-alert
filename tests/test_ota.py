import json
import unittest
from datetime import timedelta
from pathlib import Path

from flight_alert import emails, ota

from tests.helpers import NOW, small_config

FIX = Path(__file__).parent / "fixtures"


def deal(key="TPE-NRT|2026-12-10|2026-12-14", price=10000, out="TR874", back="GK13", dep="2026-12-10"):
    o, d, r = key.split("|")[0].split("-") + [key.split("|")[2]]
    return {"key": key, "route": f"{o}-{d}", "origin": o, "dest": d, "dep": dep, "ret": r, "days": 5, "adults": 2,
            "price_pp": price, "price_total": price * 2, "url": "https://g",
            "out": {"airline": out[:2], "flight": out, "dep": "14:25", "arr": "18:30"},
            "back": {"airline": back[:2], "flight": back, "dep": "08:30", "arr": "11:50"}}


class ParseTest(unittest.TestCase):
    def test_trip_return_list_gives_per_person_round_trip_prices(self):
        opts = ota.trip_options((FIX / "trip_returns.json").read_text(encoding="utf-8"))
        self.assertEqual(opts[0], {"airline": "GK", "number": "13", "airline_name": "捷星日本", "dep": "08:30",
                                   "arr": "11:50", "price_pp": 9093})

    def test_trip_sse_lines_are_parsed_too(self):
        body = "data:" + (FIX / "trip_returns.json").read_text(encoding="utf-8") + "\n\n"
        self.assertEqual(len(ota.trip_options(body)), 4)

    def test_agoda_outbound_list(self):
        opts, done, selected = ota.agoda_options((FIX / "agoda_outbound.json").read_text(encoding="utf-8"))
        self.assertTrue(done)
        self.assertFalse(selected)
        self.assertEqual(opts[0]["airline"] + opts[0]["number"], "TR874")
        self.assertEqual(opts[0]["price_pp"], 8777)

    def test_flight_matching_ignores_leading_zeros_and_spaces(self):
        self.assertTrue(ota.same_flight("GK", "13", "GK013"))
        self.assertTrue(ota.same_flight("CI", "104", "CI 104"))
        self.assertFalse(ota.same_flight("JL", "104", "CI104"))  # codeshare under another airline is not the same
        self.assertIsNone(ota.find([{"airline": "GK", "number": "11"}], {"flight": "GK13"}))


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = small_config()

    def test_picks_cheapest_future_unmuted_deals(self):
        late = (NOW.date() + timedelta(days=40)).isoformat()
        soon = (NOW.date() + timedelta(days=1)).isoformat()   # inside lead time: not checked
        ds = [deal(f"TPE-NRT|{late}|x1", 12000, dep=late), deal(f"TPE-NRT|{late}|x2", 9000, dep=late),
              deal(f"TPE-NRT|{soon}|x3", 5000, dep=soon)]
        got = ota.pick_targets(ds, self.cfg, NOW.date(), limit=5)
        self.assertEqual([d["price_pp"] for d in got], [9000, 12000])

    def test_summary_and_notify_only_when_clearly_cheaper(self):
        d = deal(price=9500)
        data = {"checked": {d["key"]: {"trip": {"status": "ok", "price_pp": 9000, "url": "t"},
                                       "agoda": {"status": "not_found", "url": "a"}, "at": NOW.isoformat()}}}
        best = ota.summarize(data["checked"][d["key"]], d["price_pp"])
        self.assertEqual((best["site"], best["saving"]), ("trip", 500))
        self.cfg.digest_max = 10000
        first = ota.to_notify([d], data, self.cfg)
        self.assertEqual(len(first), 1)
        self.assertEqual(ota.to_notify([d], data, self.cfg), [])  # same price: not again
        data["checked"][d["key"]]["trip"]["price_pp"] = 9400       # only 100 cheaper than Google
        data["notified"] = {}
        self.assertEqual(ota.to_notify([d], data, self.cfg), [])

    def test_attach_skips_stale_checks(self):
        d1, d2 = deal(price=9500), deal("TPE-NRT|2026-12-11|2026-12-15", 9500)
        data = {"checked": {
            d1["key"]: {"trip": {"status": "ok", "price_pp": 9000, "url": "t"}, "at": NOW.isoformat()},
            d2["key"]: {"trip": {"status": "ok", "price_pp": 9000, "url": "t"},
                        "at": (NOW - timedelta(hours=40)).isoformat()}}}
        ota.attach([d1, d2], data, NOW)
        self.assertEqual(d1["ota"]["best"]["price_pp"], 9000)
        self.assertNotIn("ota", d2)


class EmailTest(unittest.TestCase):
    def test_deal_shows_same_flight_ota_prices(self):
        d = deal(price=9500)
        d["ota"] = {"sites": {"trip": {"status": "ok", "price_pp": 9000, "url": "https://trip"},
                              "agoda": {"status": "not_found", "url": "https://agoda"}}}
        text = emails.deal_text(d)
        self.assertIn("Trip.com 每人 NT$9,000（比 Google 便宜 NT$500）", text)
        self.assertIn("Agoda 沒有這組航班", text)
        self.assertIn("https://trip", emails.deal_html(d))

    def test_ota_cheaper_email(self):
        d = deal(price=9500)
        d["ota"] = {"best": {"site": "trip", "name": "Trip.com", "price_pp": 9000, "url": "https://trip",
                             "saving": 500}}
        subject, text, html = emails.ota_cheaper([d], small_config())
        self.assertIn("Trip.com 比 Google 便宜", subject)
        self.assertIn("行李", text)
        self.assertIn("https://trip", html)


if __name__ == "__main__":
    unittest.main()
