import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from flight_alert import app, config, emails, lcc, store
from flight_alert.config import LccConfig

from tests.helpers import NOW, day, itin, small_config


class AirlineAwareSource:
    """Fake Google source that, like the real one, only returns flights of source.cfg.airlines."""

    def __init__(self, cfg, offers):
        from flight_alert.gflights import SourceStats
        self.cfg, self.offers, self.stats = cfg, offers, SourceStats()  # offers: [(route, dep, ret, Itinerary)]
        self.airline_sets = []

    def _ok(self, it):
        allowed = set(self.cfg.airlines)
        return it.out[0].airline in allowed and it.ret[0].airline in allowed

    def calendar(self, origin, dest, nights, start, end):
        self.stats.requests += 1
        self.airline_sets.append(tuple(self.cfg.airlines))
        out = {}
        for route, dep, ret, it in self.offers:
            if route == f"{origin}-{dest}" and start <= dep <= end and (ret - dep).days == nights and self._ok(it):
                out[(dep, ret)] = min(out.get((dep, ret), 1e9), it.total)
        return out

    def itineraries(self, origin, dest, dep, ret, outbound_ok=None, max_outbound=3):
        self.stats.requests += 1
        return [it for route, d, r, it in self.offers
                if route == f"{origin}-{dest}" and d == dep and r == ret and self._ok(it)
                and (outbound_ok is None or outbound_ok(it.out))]


def lcc_cfg(**over):
    return small_config(lcc=LccConfig(**over))


class LccConfigTest(unittest.TestCase):
    def test_defaults_and_yaml(self):
        cfg = config.parse({"lcc": {"max_total": 4800, "bag_fees": {"it": 1000}}})
        self.assertTrue(cfg.lcc.enabled)
        self.assertEqual(cfg.lcc.max_total, 4800)
        self.assertEqual(cfg.lcc.bag_fee("IT"), 1000)
        self.assertEqual(cfg.lcc.bag_fee("MM"), 950)
        self.assertEqual(cfg.lcc.bag_fee("UO"), 1200)     # not in table -> conservative default
        self.assertEqual(cfg.airlines, ["CI", "BR", "JX", "AE", "JL", "NH", "CX"])  # full-service list unchanged
        self.assertFalse(set(cfg.airlines) & set(cfg.lcc.airlines))

    def test_rejects_full_service_in_lcc_list(self):
        with self.assertRaises(config.ConfigError):
            config.parse({"lcc": {"airlines": ["IT", "BR"]}})

    def test_repo_config_loads(self):
        cfg = config.load("config.yaml")
        self.assertEqual(cfg.lcc.max_total, 5000)
        self.assertEqual(cfg.instant_max, 8000)

    def test_view_keeps_trip_rules(self):
        cfg = lcc_cfg()
        v = lcc.view(cfg)
        self.assertEqual((v.outbound, v.inbound, v.adults, v.routes()), (cfg.outbound, cfg.inbound, cfg.adults, cfg.routes()))
        self.assertEqual(v.airlines, cfg.lcc.airlines)
        self.assertEqual(v.instant_max, 5000 - 1200)  # cheapest bags: VietJet 600 x 2


class LccClassifyTest(unittest.TestCase):
    def _deal(self, out_air, back_air, price):
        return {"key": "k", "dep": "2026-11-20", "price_pp": price,
                "out": {"airline": out_air}, "back": {"airline": back_air}}

    def test_fare_plus_bags(self):
        cfg = lcc_cfg()
        d = lcc.classify([self._deal("IT", "MM", 3000)], cfg)[0]
        self.assertEqual((d["bag_pp"], d["total_pp"], d["category"]), (1900, 4900, "instant"))
        d = lcc.classify([self._deal("IT", "IT", 3100)], cfg)[0]
        self.assertEqual((d["total_pp"], d["category"]), (5000, None))   # must be < 5000
        d = lcc.classify([self._deal("UO", "UO", 2500)], cfg)[0]
        self.assertEqual((d["bag_pp"], d["category"]), (2400, "instant"))


class LccRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(store, "DATA_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        p2 = mock.patch.object(app, "send_email", side_effect=lambda s, t, h=None, dry_run=False: self.sent.append(s))
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch("flight_alert.promo.fetch", return_value=[])  # no network in tests
        p3.start()
        self.addCleanup(p3.stop)

    def offers(self):
        dep, ret = day(10), day(14)
        cheap_lcc = itin(dep, ret, 2 * 2900, out=("IT", "200", "11:00", "14:30"), back=("IT", "201", "14:00", "16:30"))
        dear_lcc = itin(day(20), day(24), 2 * 3500, out=("MM", "27", "11:00", "14:30"), back=("MM", "28", "14:00", "16:30"))
        full = itin(day(30), day(34), 2 * 7000)  # JX, full-service alert
        return [("TPE-OKA", dep, ret, cheap_lcc), ("TPE-OKA", day(20), day(24), dear_lcc),
                ("TPE-OKA", day(30), day(34), full)]

    def test_separate_emails_and_state(self):
        cfg = lcc_cfg()
        src = AirlineAwareSource(cfg, self.offers())
        app.run(cfg, "auto", source=src, now=NOW, max_requests=50)
        self.assertEqual(len(self.sent), 2, self.sent)
        full = [s for s in self.sent if s.startswith("🔥")]
        cheap = [s for s in self.sent if s.startswith("💸 廉航含行李")]
        self.assertEqual(len(full), 1)
        self.assertIn("NT$7,000", full[0])
        self.assertEqual(len(cheap), 1)
        self.assertIn("每人 NT$4,800", cheap[0])        # 2,900 + 950 x 2
        self.assertIn("票 NT$2,900＋20kg 行李約 NT$1,900", cheap[0])
        self.assertIn("台灣虎航", cheap[0])
        self.assertEqual(src.cfg.airlines, cfg.airlines)  # source config restored
        self.assertIn(tuple(cfg.lcc.airlines), src.airline_sets)
        main = store.load_state()
        side = store.load_state(lcc.STATE_FILE)
        self.assertEqual(list(main["notified"]), [f"TPE-OKA|{day(30)}|{day(34)}"])
        self.assertEqual(list(side["notified"]), [f"TPE-OKA|{day(10)}|{day(14)}"])  # MM 3,500+1,900 > 5,000
        # next run: nothing new
        self.sent.clear()
        app.run(cfg, "auto", source=AirlineAwareSource(cfg, self.offers()), now=NOW + timedelta(hours=1), max_requests=50)
        self.assertEqual(self.sent, [])

    def test_disabled_changes_nothing(self):
        cfg = small_config()
        src = AirlineAwareSource(cfg, self.offers())
        app.run(cfg, "auto", source=src, now=NOW, max_requests=50)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("🔥"))
        self.assertFalse((Path(self.tmp.name) / lcc.STATE_FILE).exists())

    def test_email_render(self):
        cfg = lcc_cfg()
        d = {"key": "TPE-OKA|2026-11-20|2026-11-24", "route": "TPE-OKA", "origin": "TPE", "dest": "OKA",
             "dep": "2026-11-20", "ret": "2026-11-24", "days": 5, "price_pp": 2900, "price_total": 5800, "adults": 2,
             "bag_pp": 1900, "total_pp": 4800, "url": "https://example.com",
             "out": {"flight": "IT200", "airline": "IT", "dep": "11:00", "arr": "14:30"},
             "back": {"flight": "IT201", "airline": "IT", "dep": "14:00", "arr": "16:30"}}
        subject, text, html = emails.lcc_instant([d], cfg)
        self.assertIn("票價 NT$2,900＋行李約 NT$1,900", text)
        self.assertIn("每人 NT$4,800", html)


if __name__ == "__main__":
    unittest.main()


class LccPromoTest(unittest.TestCase):
    def test_match(self):
        from flight_alert import promo
        L = promo.LCC_AIRLINES
        self.assertEqual(promo.match("虎航全航線999元起！飛東京大阪限時開搶", L), ["IT"])
        self.assertEqual(promo.match("廉航雙雄今開搶！台灣虎航日本特價512元起、樂桃1580元起", L), ["IT", "MM"])
        self.assertIsNone(promo.match("樂桃航空優惠｜香港飛大阪單程低至$300起", L))   # not from Taiwan
        self.assertIsNone(promo.match("華航日本線促銷76折", L))                          # full-service: not LCC
        self.assertIsNone(promo.match("虎航飛東京 營收創新高", L))
        self.assertIsNone(promo.match("虎航全航線999元起！飛東京大阪限時開搶"))          # full-service list unchanged

    def test_first_run_silent_then_email(self):
        sent = []
        send = lambda st, s, t, h, d: sent.append(s) or True  # noqa: E731
        state = store.new_state()
        item = lambda title, h: {"title": title, "source": "新聞", "link": "https://n/" + title,  # noqa: E731
                                 "published": (NOW - timedelta(hours=h)).isoformat()}
        old = [item("虎航全航線999元起！飛東京大阪限時開搶", 5)]
        lcc.check_promos(state, NOW, False, False, send, fetch=lambda: old)
        self.assertEqual(sent, [])
        new = old + [item("酷航飛東京大阪促銷 單程含稅2499元起", 1)]
        lcc.check_promos(state, NOW + timedelta(hours=1), False, False, send, fetch=lambda: new)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("🏷️ 廉航促銷：酷航"))
        lcc.check_promos(state, NOW + timedelta(hours=2), False, False, send, fetch=lambda: new)
        self.assertEqual(len(sent), 1)
