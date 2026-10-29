import unittest
from datetime import datetime, timezone, timedelta
from flight_alert.app import digest_due

TPE = timezone(timedelta(hours=8))


class Cfg:
    digest_hour = 18


def at(day, hour):
    return datetime(2026, 10, day, hour, 16, tzinfo=TPE)


class DigestDueTest(unittest.TestCase):
    def test_before_and_after_hour(self):
        self.assertFalse(digest_due(Cfg, {"digest_last_date": "2026-10-27"}, at(28, 17)))
        self.assertTrue(digest_due(Cfg, {"digest_last_date": "2026-10-27"}, at(28, 19)))

    def test_missed_window_sends_late_digest(self):
        # GitHub skipped every run between 18:00 and midnight: send yesterday's digest late.
        self.assertTrue(digest_due(Cfg, {"digest_last_date": "2026-10-27"}, at(29, 0)))

    def test_once_per_day(self):
        self.assertFalse(digest_due(Cfg, {"digest_last_date": "2026-10-28"}, at(29, 9)))
        self.assertTrue(digest_due(Cfg, {"digest_last_date": "2026-10-28"}, at(29, 18)))
        self.assertFalse(digest_due(Cfg, {"digest_last_date": "2026-10-29"}, at(29, 23)))


if __name__ == "__main__":
    unittest.main()
