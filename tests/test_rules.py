import unittest

from flight_alert import rules
from flight_alert.config import Config, ConfigError, parse
from flight_alert.links import google_flights_url

from tests.helpers import day, itin, leg


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_inside_windows_ok(self):
        ok, flags, why = rules.check(itin(day(10), day(14), 20000), self.cfg)
        self.assertTrue(ok, why)
        self.assertEqual(flags, [])

    def test_departure_window_is_exact_to_the_minute(self):
        ok, _, why = rules.check(itin(day(10), day(14), 20000, out=("CI", "122", "14:05", "16:40")), self.cfg)
        self.assertFalse(ok)
        self.assertIn("14:05", why)
        ok, _, _ = rules.check(itin(day(10), day(14), 20000, out=("CI", "122", "14:00", "16:35")), self.cfg)
        self.assertTrue(ok)

    def test_return_after_1500_rejected(self):
        ok, _, why = rules.check(itin(day(10), day(14), 20000, back=("JX", "871", "15:25", "16:10")), self.cfg)
        self.assertFalse(ok)
        self.assertIn("回程", why)

    def test_late_arrival_flagged_then_rejected(self):
        ok, flags, _ = rules.check(itin(day(10), day(14), 20000, out=("BR", "198", "13:40", "18:15")), self.cfg)
        self.assertTrue(ok)
        self.assertEqual(flags, [rules.LATE_OUT])
        ok, _, _ = rules.check(itin(day(10), day(14), 20000, out=("BR", "198", "13:50", "18:45")), self.cfg)
        self.assertFalse(ok)

    def test_return_arrival_limits(self):
        ok, flags, _ = rules.check(itin(day(10), day(14), 20000, back=("CI", "131", "14:50", "19:30")), self.cfg)
        self.assertTrue(ok)
        self.assertEqual(flags, [rules.LATE_RET])
        ok, _, _ = rules.check(itin(day(10), day(14), 20000, back=("CI", "131", "14:50", "20:10")), self.cfg)
        self.assertFalse(ok)

    def test_low_cost_and_codeshare_rejected(self):
        ok, _, _ = rules.check(itin(day(10), day(14), 9000, out=("IT", "232", "11:00", "13:30")), self.cfg)
        self.assertFalse(ok)
        it = itin(day(10), day(14), 9000)
        it.out[0].operating = "GK"
        ok, _, why = rules.check(it, self.cfg)
        self.assertFalse(ok)
        self.assertIn("執飛", why)

    def test_connection_rejected_when_nonstop_only(self):
        it = itin(day(10), day(14), 9000)
        it.out = [leg("CI", "1", "TPE", "KIX", day(10), "10:30", "14:00"), leg("CI", "2", "KIX", "OKA", day(10), "15:00", "17:00")]
        self.assertFalse(rules.check(it, self.cfg)[0])

    def test_overnight_arrival_rejected(self):
        it = itin(day(10), day(14), 9000)
        it.out[0].arr = it.out[0].arr.replace(day=it.out[0].arr.day) + (it.out[0].arr - it.out[0].arr)
        it.out[0].arr = it.out[0].dep.replace(hour=1) + __import__("datetime").timedelta(days=1)
        self.assertFalse(rules.check(it, self.cfg)[0])

    def test_best_picks_cheapest_valid(self):
        its = [
            itin(day(10), day(14), 18000, back=("JX", "871", "15:25", "16:10")),  # invalid return time
            itin(day(10), day(14), 21000),
            itin(day(10), day(14), 21000, out=("BR", "198", "13:40", "18:15")),  # same price, flagged
        ]
        best, flags = rules.best(its, self.cfg)
        self.assertEqual(best.total, 21000)
        self.assertEqual(flags, [])

    def test_classify(self):
        self.assertEqual(rules.classify(7999, self.cfg, None).category, "instant")
        self.assertEqual(rules.classify(10000, self.cfg, None).category, "digest")
        self.assertIsNone(rules.classify(12000, self.cfg, 13000).category)
        v = rules.classify(12000, self.cfg, 16000)
        self.assertEqual((v.category, v.discount_pct), ("anomaly", 25))


class ConfigTest(unittest.TestCase):
    def test_defaults_and_routes(self):
        cfg = parse({"trip": {"origins": ["TPE", "TSA"], "destinations": ["NRT", "HND", "OKA"]}})
        self.assertIn(("TSA", "HND"), cfg.routes())
        self.assertNotIn(("TSA", "OKA"), cfg.routes())
        self.assertEqual(cfg.durations, [3, 4, 5, 6, 7])
        self.assertEqual(cfg.outbound.arrive_latest, 18 * 60 + 30)

    def test_validation(self):
        with self.assertRaises(ConfigError):
            parse({"alerts": {"instant_max": 12000, "digest_max": 10000}})
        with self.assertRaises(ConfigError):
            parse({"outbound": {"depart_earliest": "15:00", "depart_latest": "10:00"}})
        with self.assertRaises(ConfigError):
            parse({"trip": {"airlines": ["IT"]}})
        with self.assertRaises(ConfigError):
            parse({"alerts": {"paused_until": "11/20"}})


class LinksTest(unittest.TestCase):
    def test_google_flights_url(self):
        import base64
        from urllib.parse import parse_qs, urlparse

        url = google_flights_url("TPE", "OKA", day(10), day(14), 2, ["CI", "JX"])
        q = parse_qs(urlparse(url).query)
        raw = base64.urlsafe_b64decode(q["tfs"][0] + "==")
        for token in (b"TPE", b"OKA", b"CI", b"JX", day(10).isoformat().encode(), day(14).isoformat().encode()):
            self.assertIn(token, raw)
        self.assertEqual(q["curr"], ["TWD"])


if __name__ == "__main__":
    unittest.main()
