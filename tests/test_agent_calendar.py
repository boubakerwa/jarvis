import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeCalendar:
    def __init__(self):
        self.event_calls = []
        self.query_calls = []

    def create_event(self, summary, start, end="", description="", location="", all_day=False):
        self.event_calls.append(
            {
                "summary": summary,
                "start": start,
                "end": end,
                "description": description,
                "location": location,
                "all_day": all_day,
            }
        )
        return {
            "id": "evt-123",
            "summary": summary,
            "start": start,
            "end": end,
            "htmlLink": "https://calendar.google.com/event?eid=evt-123",
        }

    def get_events(self, time_min, time_max, max_results=20):
        self.query_calls.append(
            {"time_min": time_min, "time_max": time_max, "max_results": max_results}
        )
        return []


class AgentCalendarTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module("tested_agent_calendar", "core/agent.py")
        self.fixed_now = datetime(2026, 4, 5, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        self.module.get_local_now = lambda: self.fixed_now

    def test_create_event_prefers_relative_date_from_user_message(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._calendar = FakeCalendar()
        agent._current_user_message = "Create a calendar entry for Monday"

        response = agent._tool_create_event({"title": "Follow up with financial advisor"})

        self.assertIn("2026-04-06", response)
        self.assertEqual(len(agent._calendar.event_calls), 1)
        call = agent._calendar.event_calls[0]
        self.assertEqual(call["start"], "2026-04-06")
        self.assertEqual(call["end"], "2026-04-07")
        self.assertTrue(call["all_day"])

    def test_check_calendar_prefers_relative_date_from_user_message(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._calendar = FakeCalendar()
        agent._current_user_message = "What do I have on Monday?"

        response = agent._tool_check_calendar({"start_date": "2025-07-21"})

        self.assertEqual(response, "No events found between 2026-04-06 and 2026-04-06.")
        self.assertEqual(len(agent._calendar.query_calls), 1)
        query = agent._calendar.query_calls[0]
        self.assertIn("2026-04-06T00:00:00", query["time_min"])


if __name__ == "__main__":
    unittest.main()
