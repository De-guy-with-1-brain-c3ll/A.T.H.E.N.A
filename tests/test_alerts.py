"""Proactive alerts: new Teams messages and weather must announce exactly once."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from athena.alerts import AlertScheduler
from athena.tools.models import ToolResult
from athena.tools.registry import ToolRegistry
from athena.watches import build_watchers, plain_text, teams_messages, weather


class FakeGraph:
    """Stands in for TeamsGraph.posts()."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    async def posts(self, team, channel, limit):
        self.calls += 1
        return self.pages.pop(0) if self.pages else []


class FakeRegistry:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def execute(self, name, arguments):
        self.calls += 1
        return self.result


def graph_message(ident, body, author="Alex", created="2026-09-15T10:00:00Z"):
    return {"id": ident, "createdDateTime": created,
            "body": {"content": body},
            "from": {"user": {"displayName": author}}}


class TeamsWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_check_only_records_history(self):
        """Creating a watch must not replay the whole channel at the user."""
        graph = FakeGraph([[graph_message("1", "old news")]])
        watch = {"params": {"team": "Class", "channel": "General"}, "state": {}}
        self.assertEqual(await teams_messages(watch, {"teams_graph": graph}), [])
        self.assertTrue(watch["state"]["baseline"])
        self.assertEqual(watch["state"]["seen"], ["1"])

    async def test_new_message_is_announced_once(self):
        graph = FakeGraph([
            [graph_message("1", "old news")],
            [graph_message("1", "old news"),
             graph_message("2", "<p>Quiz moved to Friday&nbsp;3pm</p>", author="Sam")],
            [graph_message("1", "old news"),
             graph_message("2", "<p>Quiz moved to Friday&nbsp;3pm</p>", author="Sam")],
        ])
        watch = {"params": {"team": "Class", "channel": "General"}, "state": {}}
        await teams_messages(watch, {"teams_graph": graph})
        announced = await teams_messages(watch, {"teams_graph": graph})
        self.assertEqual(len(announced), 1)
        self.assertIn("Sam", announced[0])
        self.assertIn("Quiz moved to Friday 3pm", announced[0])
        self.assertNotIn("<p>", announced[0])
        # The same post must never be announced twice.
        self.assertEqual(await teams_messages(watch, {"teams_graph": graph}), [])

    async def test_many_new_messages_are_summarised(self):
        graph = FakeGraph([
            [graph_message("1", "old")],
            [graph_message(str(index), f"note {index}") for index in range(2, 9)],
        ])
        watch = {"params": {"team": "Class", "channel": "General"}, "state": {}}
        await teams_messages(watch, {"teams_graph": graph})
        announced = await teams_messages(watch, {"teams_graph": graph})
        self.assertEqual(len(announced), 4)
        # Seven new posts: three are read out, the rest are counted.
        self.assertIn("Plus 4 more", announced[-1])

    async def test_a_failing_graph_stays_silent(self):
        class Broken:
            async def posts(self, team, channel, limit):
                raise RuntimeError("graph is down")

        watch = {"params": {"team": "Class", "channel": "General"}, "state": {}}
        self.assertEqual(await teams_messages(watch, {"teams_graph": Broken()}), [])

    async def test_without_a_teams_client_nothing_happens(self):
        watch = {"params": {"team": "Class", "channel": "General"}, "state": {}}
        self.assertEqual(await teams_messages(watch, {"teams_graph": None}), [])

    def test_plain_text_strips_markup(self):
        self.assertEqual(plain_text("<p>Hi&nbsp;there</p>\n<b>now</b>"), "Hi there now")


class WeatherWatchTests(unittest.IsolatedAsyncioTestCase):
    def _result(self, rain=10):
        return ToolResult(True, "ok", {
            "location": "Shanghai, China",
            "daily": {"temperature_2m_max": [26.4], "temperature_2m_min": [19.1],
                      "precipitation_probability_max": [rain]},
        })

    async def test_daily_briefing_fires_once_per_day(self):
        registry = FakeRegistry(self._result())
        watch = {"params": {"location": "Shanghai", "at": "00:00"}, "state": {}}
        first = await weather(watch, {"registry": registry})
        self.assertEqual(len(first), 1)
        self.assertIn("26 high, 19 low", first[0])
        self.assertIn("10 percent chance of precipitation", first[0])
        self.assertEqual(await weather(watch, {"registry": registry}), [])
        self.assertEqual(registry.calls, 1)

    async def test_rain_threshold_adds_an_umbrella_warning(self):
        registry = FakeRegistry(self._result(rain=80))
        watch = {"params": {"location": "Shanghai", "alert_if_rain_over": 60}, "state": {}}
        announced = await weather(watch, {"registry": registry})
        self.assertIn("umbrella", announced[0])

    async def test_below_the_threshold_is_a_plain_report(self):
        registry = FakeRegistry(self._result(rain=10))
        watch = {"params": {"location": "Shanghai", "alert_if_rain_over": 60}, "state": {}}
        announced = await weather(watch, {"registry": registry})
        self.assertNotIn("umbrella", announced[0])

    async def test_a_failed_forecast_stays_silent(self):
        registry = FakeRegistry(ToolResult(False, "Weather is unavailable."))
        watch = {"params": {"location": "Shanghai", "at": "00:00"}, "state": {}}
        self.assertEqual(await weather(watch, {"registry": registry}), [])


class WatchStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def _scheduler(self, notify=None):
        return AlertScheduler(notify or (lambda _text: True),
                              path=self.root / "alerts.json",
                              watchers=build_watchers())

    async def test_watch_is_persisted_and_reloaded(self):
        scheduler = self._scheduler()
        watch = scheduler.add_watch("weather", {"location": "Shanghai", "at": "07:30"},
                                    "weather for Shanghai at 07:30")
        await scheduler.close()

        reloaded = self._scheduler()
        rows = reloaded.watch_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], watch["id"])
        self.assertEqual(rows[0]["params"]["location"], "Shanghai")

    async def test_unknown_watch_kind_is_rejected(self):
        scheduler = self._scheduler()
        with self.assertRaises(ValueError):
            scheduler.add_watch("nonsense", {}, "nope")
        await scheduler.close()

    async def test_cancel_removes_one_watch(self):
        scheduler = self._scheduler()
        watch = scheduler.add_watch("weather", {"location": "Shanghai"}, "weather")
        self.assertTrue(scheduler.cancel_watch(watch["id"]))
        self.assertEqual(scheduler.watch_rows(), [])
        self.assertFalse(scheduler.cancel_watch("zzzz"))
        await scheduler.close()

    async def test_interval_has_a_floor_so_a_watch_cannot_spam(self):
        scheduler = self._scheduler()
        watch = scheduler.add_watch("weather", {"location": "Shanghai"}, "weather",
                                    interval_seconds=1)
        self.assertGreaterEqual(watch["interval_seconds"], 60)
        await scheduler.close()

    async def test_the_loop_announces_and_records_the_state(self):
        announced = []
        graph = FakeGraph([[graph_message("1", "old")],
                           [graph_message("1", "old"), graph_message("2", "brand new")]])
        scheduler = AlertScheduler(lambda text: announced.append(text) or True,
                                   teams_graph=graph, path=self.root / "alerts.json",
                                   watchers=build_watchers())
        scheduler.watch_services = {"teams_graph": graph}
        scheduler.add_watch("teams_messages", {"team": "Class", "channel": "General"},
                            "new messages", interval_seconds=60)
        await scheduler.start()
        try:
            for _ in range(60):
                await asyncio.sleep(0.05)
                if announced:
                    break
            # The first check is a baseline, so force the second pass.
            for watch in scheduler.watches.values():
                watch["next_check"] = datetime.now().astimezone().isoformat()
            for _ in range(60):
                await asyncio.sleep(0.05)
                if announced:
                    break
        finally:
            await scheduler.close()
        self.assertTrue(any("brand new" in line for line in announced), announced)

    async def test_registry_tools_create_and_list_watches(self):
        scheduler = self._scheduler()
        registry = ToolRegistry.discover(services={"settings": None,
                                                   "alert_scheduler": scheduler})
        created = await registry.execute("watch_weather", {"location": "Shanghai", "at": "07:30"})
        self.assertTrue(created.success)
        listed = await registry.execute("list_watches", {})
        self.assertIn("1 background alert", listed.spoken_text)
        watch_id = created.data["watch"]["id"]
        self.assertTrue((await registry.execute("cancel_watch", {"watch_id": watch_id})).success)
        self.assertIn("no background alerts", (await registry.execute("list_watches", {})).spoken_text)
        await scheduler.close()

    async def test_weather_watch_defaults_to_a_morning_briefing(self):
        scheduler = self._scheduler()
        registry = ToolRegistry.discover(services={"settings": None,
                                                   "alert_scheduler": scheduler})
        created = await registry.execute("watch_weather", {"location": "Shanghai"})
        self.assertTrue(created.success)
        self.assertEqual(created.data["watch"]["params"]["at"], "07:30")
        await scheduler.close()

    async def test_tools_report_unavailable_without_a_scheduler(self):
        registry = ToolRegistry.discover(services={"settings": None})
        for name, arguments in (("list_watches", {}),
                                ("watch_weather", {"location": "Shanghai"}),
                                ("watch_teams_channel", {"team": "A", "channel": "B"}),
                                ("cancel_watch", {"watch_id": "abcd"})):
            with self.subTest(name=name):
                result = await registry.execute(name, arguments)
                self.assertFalse(result.success)
                self.assertIn("unavailable", result.spoken_text)


if __name__ == "__main__":
    unittest.main()
