"""The daily Communication Journal schedule and the brief it prepares."""
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from athena.alerts import AlertScheduler
from athena.watches import build_watchers, cj_schedule, is_journal_channel


class JournalChannelTests(unittest.TestCase):
    def test_the_names_teachers_actually_use(self):
        for name in ("CJ", "cj", "Communication Journal", "Communication Journal (CJ)",
                     "COMMUNICATION JOURNAL"):
            with self.subTest(name=name):
                self.assertTrue(is_journal_channel(name))

    def test_ordinary_channels_are_not_journals(self):
        for name in ("General", "Learning Resources", "课堂资料", "QnA"):
            with self.subTest(name=name):
                self.assertFalse(is_journal_channel(name))


class DailyScheduleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scheduler = AlertScheduler(lambda _t: True,
                                        path=Path(self.directory.name) / "alerts.json")

    def test_a_watch_with_a_time_runs_once_a_day_at_that_time(self):
        watch = {"params": {"at": "16:00"}, "interval_seconds": 86400}
        # 09:00 UTC is 17:00 in Shanghai, so the next 16:00 is tomorrow.
        now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
        nxt = self.scheduler.next_check_for(watch, now)
        local = nxt.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
        self.assertEqual((local.day, local.hour, local.minute), (17, 16, 0))

    def test_before_the_time_it_runs_the_same_day(self):
        watch = {"params": {"at": "16:00"}, "interval_seconds": 86400}
        now = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)  # 09:00 Shanghai
        nxt = self.scheduler.next_check_for(watch, now)
        local = nxt.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
        self.assertEqual((local.day, local.hour), (16, 16))

    def test_without_a_time_it_falls_back_to_the_interval(self):
        now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
        nxt = self.scheduler.next_check_for({"params": {}, "interval_seconds": 300}, now)
        self.assertEqual(nxt, now + timedelta(seconds=300))

    def test_registering_the_same_watch_twice_does_not_duplicate_it(self):
        self.scheduler.watchers = build_watchers()
        first = self.scheduler.ensure_watch("cj_schedule", {"at": "16:00"}, "CJ", 86_400)
        second = self.scheduler.ensure_watch("cj_schedule", {"at": "16:00"}, "CJ", 86_400)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.scheduler.watch_rows()), 1)


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "alerts.json"
        self.scheduler = AlertScheduler(lambda _t: True, path=self.path)

    def test_a_brief_is_held_until_it_is_taken(self):
        self.scheduler.save_brief("cj_schedule", "the CJ schedule", "Maths: p42")
        rows = self.scheduler.brief_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "the CJ schedule")

        taken = self.scheduler.take_brief("cj_schedule")
        self.assertEqual(taken["text"], "Maths: p42")
        self.assertEqual(self.scheduler.brief_rows(), [])

    def test_a_brief_survives_a_restart(self):
        self.scheduler.save_brief("cj_schedule", "the CJ schedule", "Maths: p42")
        reopened = AlertScheduler(lambda _t: True, path=self.path)
        self.assertEqual(len(reopened.brief_rows()), 1)

    def test_taking_a_brief_that_is_not_there_is_harmless(self):
        self.assertIsNone(self.scheduler.take_brief("nothing"))


class _FakeTool:
    def __init__(self, payload):
        self.payload = payload

    async def execute(self, arguments):
        from athena.tools.models import ToolResult
        return ToolResult(True, "ok", self.payload)


class CjScheduleWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scheduler = AlertScheduler(lambda _t: True,
                                        path=Path(self.directory.name) / "alerts.json")
        self.scheduler.watchers = build_watchers()

    async def _run(self, teams, posts):
        class FakeGraph:
            pass

        async def channels(_self):
            return teams

        with patch("athena.tools.teams.TeamsChannelsTool",
                   lambda graph: _FakeTool({"teams": teams})), \
             patch("athena.tools.teams.TeamsPostsTool",
                   lambda graph: _FakeTool({"posts": posts})):
            return await cj_schedule({"params": {"at": "16:00"}},
                                     {"teams_graph": FakeGraph(),
                                      "alert_scheduler": self.scheduler})

    async def test_it_prepares_a_brief_and_stays_silent(self):
        teams = [{"team": "AP Physics", "channels": ["Communication Journal", "General"]}]
        posts = [{"created": "2026-09-15T00:00:00Z", "from": "Mr C",
                  "subject": "Wednesday", "text": "HW: Study for Quiz", "attachments": []}]
        spoken = await self._run(teams, posts)
        self.assertEqual(spoken, [], "the watcher must not talk to an empty room")
        briefs = self.scheduler.brief_rows()
        self.assertEqual(len(briefs), 1)
        self.assertIn("HW: Study for Quiz", briefs[0]["text"])
        self.assertIn("AP Physics", briefs[0]["text"])

    async def test_an_image_only_post_is_reported_as_unreadable(self):
        teams = [{"team": "9Gd_Chinese", "channels": ["CJ"]}]
        posts = [{"created": "2026-09-16T00:00:00Z", "from": "Ms Feng", "subject": "9.16",
                  "text": "[image: notes.png]", "attachments": []}]
        await self._run(teams, posts)
        text = self.scheduler.brief_rows()[0]["text"]
        self.assertIn("9Gd_Chinese", text)
        self.assertIn("images", text)

    async def test_nothing_to_report_means_no_brief(self):
        await self._run([{"team": "AP Physics", "channels": ["General"]}], [])
        self.assertEqual(self.scheduler.brief_rows(), [])


if __name__ == "__main__":
    unittest.main()


class WatcherWiringTests(unittest.TestCase):
    """The watcher needs the scheduler, or it silently produces nothing."""

    def test_the_registry_hands_the_scheduler_to_the_watchers(self):
        from athena.services import build_registry
        from athena.settings.store import RuntimeSettingsStore
        _, alerts = build_registry(RuntimeSettingsStore())
        # Without this the CJ watcher returned [] every afternoon and the brief
        # was never written, with no error anywhere.
        self.assertIn("alert_scheduler", alerts.watch_services)
        self.assertIs(alerts.watch_services["alert_scheduler"], alerts)
        self.assertIn("teams_graph", alerts.watch_services)
