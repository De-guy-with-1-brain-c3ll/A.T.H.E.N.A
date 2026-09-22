"""Sleep mode: short-term conversation becomes long-term memory."""
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from athena.memory.database import MemoryDatabase, StoredTurn
from athena.sleep import DEFAULT_SLEEP_MODEL, FALLBACK_MODEL, SleepCycle, day_window, local_zone


class _Completions:
    def __init__(self, payload, fail_times=0):
        self.payload = payload
        self.fail_times = fail_times
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("model unavailable")
        return NS(choices=[NS(message=NS(content=json.dumps(self.payload)))])


class _Client:
    """Stands in for AsyncOpenAI so no request leaves the machine."""

    def __init__(self, payload, fail_times=0):
        self.completions = _Completions(payload, fail_times)
        self.chat = NS(completions=self.completions)

    async def close(self):
        pass


class SleepCycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "athena.db"
        self.database = MemoryDatabase(self.path)
        self.database.initialize()

    def _add_turns(self, count=2):
        for index in range(count):
            self.database.save_turn(StoredTurn(
                uuid4(), f"question {index}", f"answer {index}"))

    def _cycle(self, payload, fail_times=0):
        client = _Client(payload, fail_times)
        with patch("athena.sleep.AsyncOpenAI", lambda **kwargs: client):
            cycle = SleepCycle(self.database, "test-key")
        cycle._client = client
        return cycle

    async def test_a_day_of_conversation_becomes_long_term_memory(self):
        self._add_turns(3)
        cycle = self._cycle({
            "summary": "Worked on the history debate chart.",
            "facts": [{"key": "history_interest", "value": "Mongol trade routes",
                       "confidence": 0.9}],
            "forget_keys": [],
        })
        report = await cycle.run()
        self.assertEqual(report.turns, 3)
        self.assertEqual(report.facts_written, 1)
        self.assertEqual(self.database.get_summary(), "Worked on the history debate chart.")
        self.assertEqual([(k, v) for k, v, _ in self.database.facts()],
                         [("history_interest", "Mongol trade routes")])

    async def test_a_day_with_no_conversation_is_skipped_not_failed(self):
        cycle = self._cycle({"summary": "x", "facts": [], "forget_keys": []})
        report = await cycle.run(date(2020, 1, 1))
        self.assertIsNotNone(report.skipped)
        self.assertIn("no conversation", report.skipped)
        self.assertEqual(self.database.get_summary(), "")

    async def test_a_dry_run_reports_without_writing(self):
        self._add_turns(1)
        cycle = self._cycle({
            "summary": "Should not be saved.",
            "facts": [{"key": "k", "value": "v", "confidence": 0.9}],
            "forget_keys": [],
        })
        report = await cycle.run(dry_run=True)
        self.assertEqual(report.facts_written, 1)
        self.assertEqual(self.database.get_summary(), "")
        self.assertEqual(self.database.facts(), [])

    async def test_the_strong_model_is_used_by_default(self):
        self._add_turns(1)
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []})
        await cycle.run()
        self.assertEqual(cycle._client.completions.calls[0]["model"], DEFAULT_SLEEP_MODEL)

    async def test_a_failing_strong_model_falls_back_instead_of_losing_the_day(self):
        self._add_turns(1)
        cycle = self._cycle({"summary": "kept", "facts": [], "forget_keys": []},
                            fail_times=1)
        report = await cycle.run()
        self.assertTrue(report.used_fallback)
        self.assertEqual(report.model, FALLBACK_MODEL)
        self.assertEqual(self.database.get_summary(), "kept")

    async def test_sensitive_facts_are_dropped_and_secrets_never_stored(self):
        self._add_turns(1)
        cycle = self._cycle({
            "summary": "Talked about accounts.",
            "facts": [
                {"key": "api_key", "value": "sk-abcdefghijklmnop", "confidence": 0.99},
                {"key": "favourite_food", "value": "ramen", "confidence": 0.9},
                {"key": "unsure", "value": "maybe something", "confidence": 0.2},
            ],
            "forget_keys": ["password"],
        })
        report = await cycle.run()
        self.assertEqual(report.facts_written, 1)
        self.assertEqual([k for k, _, _ in self.database.facts()], ["favourite_food"])

    async def test_obsolete_facts_are_forgotten(self):
        self.database.upsert_fact("old_project", "the old one", 0.9, None)
        self._add_turns(1)
        cycle = self._cycle({
            "summary": "Moved on.",
            "facts": [],
            "forget_keys": ["old_project"],
        })
        report = await cycle.run()
        self.assertEqual(report.facts_forgotten, 1)
        self.assertEqual(self.database.facts(), [])

    async def test_the_model_receives_the_whole_day_and_the_existing_memory(self):
        self.database.upsert_fact("existing", "already known", 0.9, None)
        self.database.save_summary("Yesterday's summary.")
        self._add_turns(2)
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []})
        await cycle.run()
        prompt = cycle._client.completions.calls[0]["messages"][1]["content"]
        self.assertIn("Yesterday's summary.", prompt)
        self.assertIn("existing: already known", prompt)
        self.assertIn("question 0", prompt)
        self.assertIn("question 1", prompt)

    async def test_the_report_is_readable(self):
        self._add_turns(2)
        cycle = self._cycle({
            "summary": "A summary.",
            "facts": [{"key": "k", "value": "v", "confidence": 0.9}],
            "forget_keys": [],
        })
        text = (await cycle.run()).describe()
        self.assertIn("Sleep mode consolidated", text)
        self.assertIn("short-term read : 2 turn(s)", text)
        self.assertIn("long-term facts : 1 written", text)


class DayWindowTests(unittest.TestCase):
    def test_the_window_is_the_local_day_not_utc(self):
        zone = local_zone()
        start, end = day_window(date(2026, 9, 16))
        self.assertEqual((end - start), timedelta(days=1))
        self.assertEqual(start.astimezone(zone).date(), date(2026, 9, 16))
        self.assertEqual(start.astimezone(zone).hour, 0)

    def test_todays_turns_fall_inside_todays_window(self):
        start, end = day_window(datetime.now(local_zone()).date())
        self.assertLess(start, datetime.now(timezone.utc))
        self.assertGreater(end, datetime.now(timezone.utc))
