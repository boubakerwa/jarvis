from datetime import datetime
from zoneinfo import ZoneInfo
import unittest

from core.time_utils import (
    extract_relative_date_expression,
    resolve_date_expression,
    resolve_event_time,
)


class TimeUtilsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 4, 5, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    def test_resolve_weekday_from_sunday(self):
        self.assertEqual(
            resolve_date_expression("monday", now=self.now).isoformat(),
            "2026-04-06",
        )

    def test_resolve_all_day_event_for_weekday(self):
        resolved = resolve_event_time("monday", now=self.now)
        self.assertTrue(resolved.all_day)
        self.assertEqual(resolved.start, "2026-04-06")
        self.assertEqual(resolved.end, "2026-04-07")

    def test_resolve_timed_event_for_weekday(self):
        resolved = resolve_event_time("monday at 3pm", now=self.now)
        self.assertFalse(resolved.all_day)
        self.assertIn("2026-04-06T15:00:00", resolved.start)

    def test_extract_relative_date_expression(self):
        self.assertEqual(
            extract_relative_date_expression("What do I have on Monday?"),
            "Monday",
        )


if __name__ == "__main__":
    unittest.main()
