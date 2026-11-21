import unittest
from datetime import datetime

from lantern.local_time import central_time


class CentralTimeTests(unittest.TestCase):
    def test_chicago_dst_transitions_and_local_date(self):
        cases = {
            "2026-03-08T07:59:59Z": "2026-03-08 01:59:59 AM CST (UTC-06:00)",
            "2026-03-08T08:00:00Z": "2026-03-08 03:00:00 AM CDT (UTC-05:00)",
            "2026-11-01T06:30:00Z": "2026-11-01 01:30:00 AM CDT (UTC-05:00)",
            "2026-11-01T07:30:00Z": "2026-11-01 01:30:00 AM CST (UTC-06:00)",
            "2027-01-01T00:00:00Z": "2026-12-31 06:00:00 PM CST (UTC-06:00)",
        }
        for instant, expected in cases.items():
            with self.subTest(instant=instant):
                self.assertEqual(central_time(instant), expected)
                self.assertEqual(central_time(datetime.fromisoformat(instant)), expected)

    def test_missing_source_timezone_is_rejected(self):
        with self.assertRaises(ValueError):
            central_time("2026-11-01T01:30:00")
