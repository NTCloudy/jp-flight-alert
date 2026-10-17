import unittest
from datetime import timedelta

from flight_alert import scanner, store
from flight_alert.gflights import RateLimited

from tests.helpers import NOW, FakeSource, day, itin, small_config


def fresh_state():
    return store.new_state()


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = small_config()
        self.dep, self.ret = day(20), day(24)
        cheap = {(self.dep, self.ret): 15000.0, (day(30), day(34)): 26000.0}
        self.src = FakeSource(
            calendar_prices={"TPE-OKA": cheap, "TPE-NRT": {(day(10), day(14)): 30000.0}},
            itins={
                ("TPE-OKA", self.dep, self.ret): [itin(self.dep, self.ret, 15000.0)],
                ("TPE-NRT", day(10), day(14)): [itin(day(10), day(14), 30000.0, origin="TPE", dest="NRT")],
            },
        )

    def test_first_run_collects_and_classifies(self):
        state, cal, hist = fresh_state(), {}, {}
        res = scanner.run(self.cfg, self.src, state, cal, hist, NOW, max_requests=100)
        self.assertTrue(res.ok)
        self.assertEqual(res.routes["TPE-OKA"]["min_pp"], 7500)
        deal = next(d for d in res.deals if d["route"] == "TPE-OKA")
        self.assertEqual(deal["category"], "instant")
        self.assertEqual(deal["price_pp"], 7500)
        self.assertEqual(deal["days"], 5)
        # NRT 15,000/person is above every threshold, but its cheapest itinerary is still verified for the table
        nrt = next(d for d in res.deals if d["route"] == "TPE-NRT")
        self.assertIsNone(nrt["category"])
        self.assertIn("TPE-OKA", hist)

    def test_verification_is_cached_until_price_changes(self):
        state, cal, hist = fresh_state(), {}, {}
        scanner.run(self.cfg, self.src, state, cal, hist, NOW, max_requests=100)
        calls = len(self.src.itinerary_calls)
        scanner.run(self.cfg, self.src, state, cal, hist, NOW + timedelta(hours=1), max_requests=100)
        self.assertEqual(len(self.src.itinerary_calls), calls)  # nothing due, cache reused
        self.src.calendar_prices["TPE-OKA"][(self.dep, self.ret)] = 14000.0
        self.src.itins[("TPE-OKA", self.dep, self.ret)] = [itin(self.dep, self.ret, 14000.0)]
        res = scanner.run(self.cfg, self.src, state, cal, hist, NOW + timedelta(hours=4), max_requests=100)
        self.assertGreater(len(self.src.itinerary_calls), calls)
        self.assertEqual(next(d for d in res.deals if d["route"] == "TPE-OKA")["price_pp"], 7000)

    def test_invalid_itinerary_not_reported(self):
        self.src.itins[("TPE-OKA", self.dep, self.ret)] = [itin(self.dep, self.ret, 15000.0, back=("JX", "871", "15:25", "16:10"))]
        res = scanner.run(self.cfg, self.src, fresh_state(), {}, {}, NOW, max_requests=100)
        self.assertFalse([d for d in res.deals if d["key"].startswith("TPE-OKA|" + self.dep.isoformat())])

    def test_rate_limit_stops_and_sets_cooldown(self):
        src = FakeSource(calendar_prices=self.src.calendar_prices, rate_limit_after=1)
        state = fresh_state()
        res = scanner.run(self.cfg, src, state, {}, {}, NOW, max_requests=100)
        self.assertTrue(res.rate_limited)
        self.assertFalse(res.ok)
        self.assertEqual(src.stats.requests, 2)
        self.assertIsNotNone(state["cooldown_until"])
        # next run inside the cooldown does not touch the network
        src2 = FakeSource(calendar_prices=self.src.calendar_prices)
        res2 = scanner.run(self.cfg, src2, state, {}, {}, NOW + timedelta(minutes=10), max_requests=100)
        self.assertTrue(res2.skipped_cooldown)
        self.assertEqual(src2.stats.requests, 0)

    def test_budget_respected_and_resumes(self):
        cfg = small_config(min_days=4, max_days=6, horizon_days=150)
        src = FakeSource(calendar_prices=self.src.calendar_prices, itins=self.src.itins)
        state, cal = fresh_state(), {}
        res = scanner.run(cfg, src, state, cal, {}, NOW, max_requests=5)
        self.assertLessEqual(src.stats.requests, 5)
        self.assertGreater(res.tasks_due, 0)
        before = res.tasks_due
        res2 = scanner.run(cfg, src, state, cal, {}, NOW + timedelta(minutes=60), max_requests=5)
        self.assertLess(res2.tasks_due, before)

    def test_preferred_trip_length_first(self):
        cfg = small_config(min_days=4, max_days=6, horizon_days=60)
        tasks = scanner.plan(cfg, {}, fresh_state(), NOW)
        self.assertEqual({t.nights for t in tasks[:2]}, {4})  # 5-day trips first for both routes

    def test_no_service_needs_two_empty_passes(self):
        cfg = small_config(destinations=["HKD"], horizon_days=60)
        src = FakeSource()
        state, cal = fresh_state(), {}
        scanner.run(cfg, src, state, cal, {}, NOW, max_requests=50)
        self.assertNotIn("TPE-HKD", state["routes"])
        scanner.run(cfg, src, state, cal, {}, NOW + timedelta(hours=4), max_requests=50)
        self.assertIn("TPE-HKD", state["routes"])
        res = scanner.run(cfg, src, state, cal, {}, NOW + timedelta(hours=8), max_requests=50)
        self.assertTrue(res.routes["TPE-HKD"]["no_service"])

    def test_failed_calendar_keeps_task_due(self):
        src = FakeSource(calendar_prices=self.src.calendar_prices, fail_routes={"TPE-NRT"})
        state, cal = fresh_state(), {}
        res = scanner.run(self.cfg, src, state, cal, {}, NOW, max_requests=100)
        self.assertNotIn("TPE-NRT", cal)
        self.assertTrue(any(t.route == "TPE-NRT" for t in scanner.plan(self.cfg, cal, state, NOW)))
        self.assertTrue(res.ok)  # one failure out of several requests

    def test_sudden_empty_answer_keeps_previous_prices(self):
        state, cal = fresh_state(), {}
        scanner.run(self.cfg, self.src, state, cal, {}, NOW, max_requests=100)
        self.src.calendar_prices["TPE-OKA"] = {}  # Google glitch: "no flights" for a priced window
        res = scanner.run(self.cfg, self.src, state, cal, {}, NOW + timedelta(hours=4), max_requests=100)
        self.assertEqual(res.suspect_empty, 1)
        self.assertEqual(res.routes["TPE-OKA"]["min_pp"], 7500)

    def test_near_miss_reason_is_reported(self):
        self.src.itins[("TPE-OKA", self.dep, self.ret)] = [itin(self.dep, self.ret, 15000.0, back=("JX", "871", "15:25", "16:10"))]
        self.src.calendar_prices["TPE-OKA"] = {(self.dep, self.ret): 15000.0}
        res = scanner.run(self.cfg, self.src, fresh_state(), {}, {}, NOW, max_requests=100)
        self.assertIn("15:25", res.routes["TPE-OKA"]["near_miss"]["why"])


if __name__ == "__main__":
    unittest.main()
