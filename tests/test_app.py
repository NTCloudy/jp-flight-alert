import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from flight_alert import app, emails, promo, store
from flight_alert.config import Config

from tests.helpers import NOW, FakeSource, day, itin, small_config


def deal(key="TPE-OKA|2026-11-20|2026-11-24", price=7500, category="instant"):
    route, dep, ret = key.split("|")
    o, d = route.split("-")
    return {"key": key, "route": route, "origin": o, "dest": d, "dest_name": "沖繩", "dep": dep, "ret": ret, "days": 5,
            "price_pp": price, "price_total": price * 2, "adults": 2,
            "out": {"flight": "JX870", "airline": "JX", "from": o, "to": d, "dep": "11:40", "arr": "14:15"},
            "back": {"flight": "JX871", "airline": "JX", "from": d, "to": o, "dep": "14:25", "arr": "15:10"},
            "flags": [], "verified_at": NOW.isoformat(), "url": "https://example.com", "category": category,
            "discount_pct": None}


class PickInstantTest(unittest.TestCase):
    def test_dedupe_drop_and_return(self):
        cfg, state = Config(), store.new_state()
        self.assertEqual(len(app.pick_instant([deal()], state, cfg, NOW)), 1)
        self.assertEqual(app.pick_instant([deal()], state, cfg, NOW + timedelta(hours=1)), [])
        self.assertEqual(app.pick_instant([deal(price=7300)], state, cfg, NOW + timedelta(hours=2)), [])  # only -200
        self.assertEqual(len(app.pick_instant([deal(price=7100)], state, cfg, NOW + timedelta(hours=3))), 1)
        # disappears for >12h, then comes back -> notify again
        app.pick_instant([], state, cfg, NOW + timedelta(hours=4))
        self.assertEqual(app.pick_instant([deal(price=7100)], state, cfg, NOW + timedelta(hours=8)), [])
        app.pick_instant([], state, cfg, NOW + timedelta(hours=9))
        self.assertEqual(len(app.pick_instant([deal(price=7100)], state, cfg, NOW + timedelta(hours=22))), 1)

    def test_muted_and_non_instant_ignored(self):
        cfg = Config(muted=["TPE-OKA|2026-11-20|2026-11-24"])
        state = store.new_state()
        self.assertEqual(app.pick_instant([deal(), deal("TPE-NRT|2026-11-20|2026-11-24", 9000, "digest")], state, cfg, NOW), [])


class AppRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(store, "DATA_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = []
        p2 = mock.patch.object(app, "send_email", side_effect=lambda s, t, h=None, dry_run=False: self.sent.append(s))
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(promo, "fetch", return_value=[])
        p3.start()
        self.addCleanup(p3.stop)

    def _source(self):
        dep, ret = day(20), day(24)
        return FakeSource(calendar_prices={"TPE-OKA": {(dep, ret): 15000.0}},
                          itins={("TPE-OKA", dep, ret): [itin(dep, ret, 15000.0)]})

    def test_instant_then_digest_once_per_day(self):
        cfg = small_config(destinations=["OKA"], digest_hour=8)
        app.run(cfg, "auto", source=self._source(), now=NOW, max_requests=50)
        self.assertTrue(any(s.startswith("🔥") for s in self.sent))
        self.assertFalse(any(s.startswith("✈️") for s in self.sent))  # first digest: next morning
        n = len(self.sent)
        app.run(cfg, "auto", source=self._source(), now=NOW + timedelta(hours=1), max_requests=50)
        self.assertEqual(len(self.sent), n)  # same deal, nothing new
        morning = NOW.replace(hour=8, minute=41) + timedelta(days=1)
        app.run(cfg, "auto", source=self._source(), now=morning, max_requests=50)
        self.assertTrue(any(s.startswith("✈️ 每日機票整理") for s in self.sent))
        n = len(self.sent)
        app.run(cfg, "auto", source=self._source(), now=morning + timedelta(hours=1), max_requests=50)
        self.assertEqual(len(self.sent), n)  # digest only once a day
        latest = store.load("latest.json", {})
        self.assertEqual(latest["deals"][0]["price_pp"], 7500)
        self.assertTrue((Path(self.tmp.name) / "calendar.json").exists())

    def test_paused_sends_nothing(self):
        cfg = small_config(destinations=["OKA"], paused_until=(NOW.date() + timedelta(days=3)))
        app.run(cfg, "auto", source=self._source(), now=NOW, max_requests=50)
        self.assertEqual(self.sent, [])

    def test_digest_before_hour_not_sent(self):
        cfg = small_config(destinations=["OKA"], digest_hour=23)
        app.run(cfg, "auto", source=self._source(), now=NOW, max_requests=50)
        self.assertFalse(any(s.startswith("✈️") for s in self.sent))


class PromoTest(unittest.TestCase):
    def test_match(self):
        self.assertEqual(promo.match("星宇航空線上旅展來了！北中南出發全航線85折起　飛日本萬元有找"), ["JX"])
        self.assertEqual(promo.match("國泰航空推線上旅展全航線8折！飛東京僅1萬3千多元"), ["CX"])
        self.assertIsNone(promo.match("懶人包》2026飛行卡/航空卡/哩程卡刷卡優惠比較"))
        self.assertIsNone(promo.match("台灣虎航日本線促銷 單程999元起"))  # low-cost carrier only
        self.assertIsNone(promo.match("華航第三季營收創新高"))
        self.assertIsNone(promo.match("國泰世華CUBE卡日本旅遊優惠"))

    def test_select_dedupes_same_sale(self):
        now = NOW
        items = [
            {"title": "星宇航空旅展全航線85折起 飛日本萬元有找", "link": "a", "source": "A", "published": now.isoformat()},
            {"title": "ITF旅展開跑 星宇全航線85折起、抽商務艙", "link": "b", "source": "B", "published": now.isoformat()},
            {"title": "國泰航空推線上旅展全航線8折！飛東京僅1萬3千多元", "link": "c", "source": "C", "published": now.isoformat()},
        ]
        picked = promo.select(items, {}, now)
        self.assertEqual([p["link"] for p in picked], ["a", "c"])
        state = store.new_state()
        promo.update_state(state, picked, now)
        self.assertEqual(len(state["promo_pending"]), 2)
        self.assertEqual(promo.select(items, state["promo_seen"], now, recent=state["promo_recent"]), [])

    def test_parse_rss(self):
        xml = ("<rss><channel><item><title>星宇日本線促銷 - 自由時報</title><link>https://x</link>"
               "<pubDate>Fri, 16 Oct 2026 08:00:00 GMT</pubDate><source url='https://ltn'>自由時報</source></item>"
               "</channel></rss>")
        items = promo.parse_rss(xml)
        self.assertEqual(items[0]["title"], "星宇日本線促銷")
        self.assertEqual(items[0]["source"], "自由時報")


class EmailsTest(unittest.TestCase):
    def test_instant_and_digest_render(self):
        cfg = Config()
        s, t, h = emails.instant([deal(), deal("TPE-NRT|2026-12-01|2026-12-05", 7900)], cfg)
        self.assertIn("2 筆超便宜機票", s)
        self.assertIn("JX870", t)
        self.assertIn("NT$7,500", h)
        latest = {"deals": [deal(), deal("TPE-NRT|2026-12-01|2026-12-05", 9500, "digest"),
                            {**deal("TPE-CTS|2026-12-01|2026-12-05", 12800, "anomaly"), "discount_pct": 22}],
                  "routes": {"TPE-OKA": {"origin": "TPE", "dest": "OKA", "min_pp": 7500, "min_dep": "2026-11-20"}},
                  "stats": {"requests_24h": 100, "success_rate_24h": "98%"}, "coverage": 0.8}
        s, t, h = emails.digest(cfg, latest, [{"title": "星宇85折", "link": "x", "source": "A"}], NOW)
        self.assertIn("超便宜 1 筆", s)
        self.assertIn("1萬內 1 筆", s)
        self.assertIn("特別便宜 1 筆", s)
        self.assertIn("促銷 1 則", s)
        self.assertIn("比這條航線平常便宜 22%", t)
        empty = emails.digest(cfg, {"deals": [], "routes": {}}, [], NOW)
        self.assertIn("今天沒有符合門檻的機票", empty[0])


if __name__ == "__main__":
    unittest.main()
