"""Alarms must parse natural speech, persist, fire, and never be faked."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

from athena.alerts import (
    AlertScheduler,
    describe_clock,
    describe_due,
    describe_remaining,
    parse_amount,
)
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.services import build_registry
from athena.settings.store import RuntimeSettingsStore
from athena.tools.alarms import (
    UNAVAILABLE,
    AlarmTool,
    CancelAlarmTool,
    ListAlarmsTool,
)
from athena.tools.registry import ToolRegistry
from athena.watches import build_watchers


SHANGHAI = ZoneInfo("Asia/Shanghai")


def chunk(text=None, calls=None):
    return NS(choices=[NS(delta=NS(content=text, tool_calls=calls))])


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for item in self.chunks:
            yield item


class ParseWhenTests(unittest.TestCase):
    """The local alarm path used to strip punctuation before parsing the time."""

    def test_spoken_numbers_are_understood(self):
        for text, minutes in (("10 minutes", 10), ("ten minutes", 10),
                              ("thirty minutes", 30), ("thirty-five minutes", 35),
                              ("in ten minutes", 10), ("twenty five minutes", 25),
                              ("twenty-five minutes", 25), ("half an hour", 30),
                              ("an hour", 60), ("a minute", 1),
                              ("one and a half hours", 90)):
            with self.subTest(text=text):
                due = AlertScheduler.parse_when(text)
                seconds = (due - AlertScheduler.parse_when("0 seconds")).total_seconds()
                self.assertAlmostEqual(seconds, minutes * 60, delta=90)

    def test_clock_times_survive_missing_punctuation(self):
        """normalize_command() turns "7:30 pm" into "7 30 pm"; both must parse."""
        now = datetime(2026, 9, 15, 1, 0, 0, tzinfo=timezone.utc)  # 09:00 Shanghai
        for text in ("9 pm", "9pm", "9 p.m.", "9 p m", "9:00 PM", "at 9 pm"):
            with self.subTest(text=text):
                local = AlertScheduler.parse_when(text, now=now).astimezone(SHANGHAI)
                self.assertEqual((local.hour, local.minute), (21, 0))

    def test_trailing_sentence_punctuation_is_ignored(self):
        # A fixed now, because two calls with no argument each read the clock and
        # the microseconds differ under load. That made this pass or fail by luck.
        now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(AlertScheduler.parse_when("ten seconds.", now=now),
                         AlertScheduler.parse_when("ten seconds", now=now))

    def test_named_times(self):
        now = datetime(2026, 9, 15, 1, 0, 0, tzinfo=timezone.utc)
        noon = AlertScheduler.parse_when("noon", now=now).astimezone(SHANGHAI)
        self.assertEqual((noon.hour, noon.minute), (12, 0))

    def test_nonsense_is_rejected(self):
        for text in ("", "whenever", "next tuesday", "soon"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    AlertScheduler.parse_when(text)

    def test_parse_amount_reads_words_and_digits(self):
        self.assertEqual(parse_amount("10"), 10.0)
        self.assertEqual(parse_amount("ten"), 10.0)
        self.assertEqual(parse_amount("half"), 0.5)
        self.assertIsNone(parse_amount("nonsense"))

    def test_describe_due_reports_the_stored_instant(self):
        now = datetime(2026, 9, 15, 13, 0, 0, tzinfo=timezone.utc)  # 21:00 Shanghai
        self.assertEqual(describe_due(now + timedelta(minutes=10), now), "in 10 minutes")
        self.assertEqual(describe_due(now + timedelta(seconds=20), now), "in 20 seconds")
        self.assertEqual(describe_due(now + timedelta(hours=2), now), "at 11:00 PM today")
        self.assertEqual(describe_due(now + timedelta(days=1, hours=2), now), "at 11:00 PM tomorrow")
        self.assertEqual(describe_due(now + timedelta(days=3), now), "at 9:00 PM on Friday")


class AlarmRegistryTests(unittest.IsolatedAsyncioTestCase):
    """Every interface must hand the alarm tools a started scheduler."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    async def _registry(self):
        fired = []
        alerts = AlertScheduler(lambda text: fired.append(text) or True,
                                path=self.root / "alerts.json")
        registry = ToolRegistry.discover(services={"settings": None,
                                                   "alert_scheduler": alerts})
        await alerts.start()
        self.addAsyncCleanup(alerts.close)
        return registry, alerts, fired

    async def test_the_utterances_that_failed_on_the_pi_now_work(self):
        registry, alerts, _ = await self._registry()
        for utterance in ("Set an alarm for ten seconds.",
                          "set an alarm for 9 p.m.",
                          "set an alarm for 9 pm",
                          "set an alarm at 7:30 pm",
                          "set an alarm for 9:00 PM",
                          "set a timer for 5 minutes",
                          "remind me in 10 minutes to study"):
            with self.subTest(utterance=utterance):
                result = await registry.handle_user_command(utterance)
                self.assertIsNotNone(result, "alarm request was not handled locally")
                self.assertTrue(result.success, result.spoken_text)
        self.assertEqual(len(alerts.rows()), 7)
        await alerts.close()

    async def test_time_is_read_before_punctuation_is_stripped(self):
        """Regression: the stripped form "9 p m" must not be what gets parsed."""
        registry, alerts, _ = await self._registry()
        self.assertEqual(registry.normalize_command("set an alarm at 7:30 pm"),
                         "set an alarm at 7 30 pm")
        self.assertEqual(registry.soften_command("set an alarm at 7:30 pm"),
                         "set an alarm at 7:30 pm")
        result = await registry.handle_user_command("set an alarm at 7:30 pm")
        self.assertTrue(result.success)
        self.assertEqual(len(alerts.rows()), 1)
        await alerts.close()

    async def test_alarm_is_persisted_and_reloaded(self):
        registry, alerts, _ = await self._registry()
        await registry.handle_user_command("remind me in 10 minutes to call mum")
        await alerts.close()

        reloaded = AlertScheduler(lambda _text: True, path=self.root / "alerts.json")
        rows = reloaded.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "call mum")

    async def test_alarm_fires_through_the_notify_callback(self):
        registry, alerts, fired = await self._registry()
        await registry.execute("set_alarm", {"when": "1 second", "message": "stand up"})
        await asyncio.sleep(2.2)
        self.assertEqual(fired, ["Alarm: stand up"])
        self.assertEqual(alerts.rows(), [], "a fired alarm must not linger")
        await alerts.close()

    async def test_list_alarms_says_unavailable_instead_of_empty(self):
        """Without a scheduler an empty list is a lie, not a healthy store."""
        registry = ToolRegistry.discover(services={"settings": None})
        listed = await registry.execute("list_alarms", {})
        self.assertFalse(listed.success)
        self.assertEqual(listed.spoken_text, UNAVAILABLE)
        set_result = await registry.execute("set_alarm", {"when": "10 minutes", "message": "x"})
        self.assertFalse(set_result.success)
        self.assertEqual(set_result.spoken_text, UNAVAILABLE)

    async def test_build_registry_shares_one_started_scheduler(self):
        registry, alerts = build_registry(None, notify=lambda _text: True)
        self.assertIs(registry.get("set_alarm").scheduler, alerts)
        self.assertIs(registry.get("list_alarms").scheduler, alerts)
        self.assertIs(registry.get("cancel_alarm").scheduler, alerts)
        await alerts.close()

    async def test_cancel_removes_the_alarm(self):
        registry, alerts, _ = await self._registry()
        result = await registry.execute("set_alarm", {"when": "1 hour", "message": "tea"})
        alarm_id = result.data["alarm"]["id"]
        self.assertTrue((await registry.execute("cancel_alarm", {"alarm_id": alarm_id})).success)
        self.assertEqual(alerts.rows(), [])
        await alerts.close()


class AlarmHonestyTests(unittest.IsolatedAsyncioTestCase):
    """The model must never confirm an alarm the hub did not store."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def _model(self, registry):
        return DeepSeekLanguageModel("test", "test", registry,
                                     RuntimeSettingsStore(self.root / "settings.json"))

    async def test_fabricated_confirmation_is_replaced_with_the_truth(self):
        alerts = AlertScheduler(lambda _text: True, path=self.root / "alerts.json")
        registry = ToolRegistry.discover(services={"settings": None, "alert_scheduler": alerts})
        model = self._model(registry)
        claim = "Done. Your 9 PM study alarm is set again."
        model._client.chat.completions.create = AsyncMock(
            side_effect=[Stream([chunk(claim)]), Stream([chunk(claim)])])
        try:
            answer = "".join([part async for part in model.stream_reply(uuid4(), "yes, do it again")])
            self.assertNotIn("is set again", answer)
            self.assertIn("verified completion receipt", answer)
            self.assertEqual(alerts.rows(), [])
        finally:
            await model.close()
            await alerts.close()

    async def test_a_real_alarm_call_is_not_suppressed(self):
        alerts = AlertScheduler(lambda _text: True, path=self.root / "alerts.json")
        registry = ToolRegistry.discover(services={"settings": None, "alert_scheduler": alerts})
        model = self._model(registry)
        call = NS(index=0, id="call_1", function=NS(
            name="set_alarm",
            arguments=json.dumps({"when": "10 minutes", "message": "study"})))
        model._client.chat.completions.create = AsyncMock(side_effect=[
            Stream([chunk(calls=[call])]),
            Stream([chunk("Your study alarm is set.")]),
        ])
        try:
            answer = "".join([part async for part in model.stream_reply(uuid4(), "set an alarm for 10 minutes")])
            # A ten minute request is confirmed as a timer, with a countdown.
            self.assertIn("set:", answer.casefold())
            self.assertIn("10 minutes from now", answer)
            self.assertEqual(len(alerts.rows()), 1)
        finally:
            await model.close()
            await alerts.close()

    async def test_an_existing_alarm_makes_the_claim_true(self):
        alerts = AlertScheduler(lambda _text: True, path=self.root / "alerts.json")
        alerts.add("1 hour", "lecture")
        registry = ToolRegistry.discover(services={"settings": None, "alert_scheduler": alerts})
        model = self._model(registry)
        listed = NS(index=0, id='alarms', function=NS(name='list_alarms', arguments='{}'))
        model._client.chat.completions.create = AsyncMock(
            side_effect=[Stream([chunk(calls=[listed])]), Stream([chunk("Your lecture alarm is set.")])])
        try:
            answer = "".join([part async for part in model.stream_reply(uuid4(), "is the alarm set?")])
            self.assertIn("is set", answer)
        finally:
            await model.close()
            await alerts.close()

    async def test_honest_failure_is_not_rewritten(self):
        alerts = AlertScheduler(lambda _text: True, path=self.root / "alerts.json")
        registry = ToolRegistry.discover(services={"settings": None, "alert_scheduler": alerts})
        model = self._model(registry)
        model._client.chat.completions.create = AsyncMock(
            side_effect=[Stream([chunk("I couldn't set that alarm.")])])
        try:
            answer = "".join([part async for part in model.stream_reply(uuid4(), "handle that for me")])
            self.assertIn("couldn't set", answer)
        finally:
            await model.close()
            await alerts.close()

    async def test_an_alarm_request_without_a_time_asks_instead_of_inventing_one(self):
        alerts = AlertScheduler(lambda _text: True, path=self.root / "alerts.json")
        registry = ToolRegistry.discover(services={"settings": None, "alert_scheduler": alerts})
        model = self._model(registry)
        model._client.chat.completions.create = AsyncMock(return_value=Stream([chunk('What time should I set it for?')]))
        try:
            answer = "".join([part async for part in model.stream_reply(uuid4(), "set an alarm")])
            self.assertIn("What time", answer)
            model._client.chat.completions.create.assert_awaited_once()
            self.assertEqual(alerts.rows(), [])
        finally:
            await model.close()
            await alerts.close()


class SharedAlarmStoreTests(unittest.IsolatedAsyncioTestCase):
    """Several interfaces share one alerts.json; none may erase another's work.

    The voice service, the dashboard and the Feishu connector each run their own
    scheduler over the same file. Before the store was made multi-process safe,
    whichever process saved last overwrote the others, so an alarm set by voice
    was silently erased by the dashboard's next periodic save.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "alerts.json"

    def _scheduler(self, notify=None):
        return AlertScheduler(notify or (lambda _text: True), path=self.path)

    def test_another_interfaces_save_does_not_erase_an_alarm(self):
        voice = self._scheduler()
        dashboard = self._scheduler()
        voice.add("10 minutes", "call mum")
        # The dashboard periodically writes its own state (Teams polling).
        dashboard._merge_teams_notified({"assignment-1": "2026-09-20T00:00:00Z"})
        self.assertEqual([row["message"] for row in voice.rows()], ["call mum"])

    def test_an_alarm_set_in_one_interface_is_visible_in_another(self):
        voice = self._scheduler()
        dashboard = self._scheduler()
        voice.add("10 minutes", "call mum")
        self.assertEqual([row["message"] for row in dashboard.rows()], ["call mum"])

    def test_a_cancel_in_one_interface_sticks_in_another(self):
        voice = self._scheduler()
        dashboard = self._scheduler()
        alarm = voice.add("1 hour", "tea")
        self.assertTrue(dashboard.cancel(alarm["id"]))
        self.assertEqual(voice.rows(), [], "a cancelled alarm came back")

    def test_a_due_alarm_is_claimed_by_exactly_one_interface(self):
        first = self._scheduler()
        second = self._scheduler()
        first.add("1 second", "stand up")
        claimed = first.claim_due_alarms()
        # Nothing is due yet, so the second interface must also find nothing.
        self.assertEqual(len(claimed) + len(second.claim_due_alarms()), 0)

        past = datetime.now(timezone.utc) + timedelta(seconds=5)
        self.assertEqual(len(first.claim_due_alarms(now=past)), 1)
        self.assertEqual(len(second.claim_due_alarms(now=past)), 0,
                         "two interfaces claimed the same alarm")

    def test_simultaneous_writes_from_two_interfaces_keep_both_alarms(self):
        """The real race: both save in the same instant."""
        first = self._scheduler()
        second = self._scheduler()
        barrier = threading.Barrier(2)

        def write(scheduler, message):
            barrier.wait(timeout=10)
            scheduler.add("10 minutes", message)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, first, "first"),
                       pool.submit(write, second, "second")]
            for future in futures:
                future.result(timeout=15)

        self.assertEqual(sorted(row["message"] for row in first.rows()),
                         ["first", "second"])

    def test_watches_are_shared_too(self):
        first = self._scheduler()
        second = self._scheduler()
        first.watchers = build_watchers()
        second.watchers = build_watchers()
        first.add_watch("weather", {"location": "Shanghai", "at": "07:30"}, "weather")
        self.assertEqual(len(second.watch_rows()), 1)

    def test_a_due_watch_is_claimed_by_exactly_one_interface(self):
        first = self._scheduler()
        second = self._scheduler()
        first.watchers = build_watchers()
        second.watchers = build_watchers()
        first.add_watch("weather", {"location": "Shanghai"}, "weather",
                        interval_seconds=60)
        # add_watch leaves next_check unset, so it is due immediately.
        self.assertEqual(len(first._claim_due_watches()), 1)
        self.assertEqual(len(second._claim_due_watches()), 0,
                         "two interfaces ran the same watch")


class AlarmPhraseTests(unittest.IsolatedAsyncioTestCase):
    """Phrasings that used to escape the local path and reach the model."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.alerts = AlertScheduler(lambda _text: True, path=self.root / "alerts.json")
        self.registry = ToolRegistry.discover(
            services={"settings": None, "alert_scheduler": self.alerts})

    async def test_asking_what_is_scheduled_stays_local(self):
        """The model has no alarm state, so these used to rely on its memory."""
        self.alerts.add("10 minutes", "call mum")
        for phrase in ("what alarms do I have", "do I have any alarms",
                       "any alarms", "list my alarms", "show alarms"):
            with self.subTest(phrase=phrase):
                result = await self.registry.handle_user_command(phrase)
                self.assertIsNotNone(result, f"{phrase!r} fell through to the model")
                self.assertIn("call mum", result.spoken_text)
                self.assertIn("minutes", result.spoken_text)

    async def test_an_unknown_alarm_id_is_refused_instead_of_invented(self):
        result = await self.registry.handle_user_command("cancel alarm zzzzzzzz")
        self.assertIsNotNone(result, "an unmatched cancel fell through to the model")
        self.assertFalse(result.success)
        self.assertIn("could not find", result.spoken_text.casefold())

    async def test_a_real_alarm_id_cancels(self):
        alarm = self.alerts.add("10 minutes", "call mum")
        result = await self.registry.handle_user_command(f"cancel alarm {alarm['id']}")
        self.assertTrue(result.success)
        self.assertEqual(self.alerts.rows(), [])

    def test_tomorrow_morning_is_understood(self):
        zone = ZoneInfo("Asia/Shanghai")
        fixed = datetime(2026, 9, 16, 15, 0, tzinfo=zone).astimezone(timezone.utc)
        local = AlertScheduler.parse_when("tomorrow morning", now=fixed).astimezone(zone)
        self.assertEqual((local.day, local.hour, local.minute), (17, 9, 0))

    def test_tomorrow_with_a_clock_keeps_the_clock(self):
        zone = ZoneInfo("Asia/Shanghai")
        fixed = datetime(2026, 9, 16, 15, 0, tzinfo=zone).astimezone(timezone.utc)
        local = AlertScheduler.parse_when("tomorrow at 9", now=fixed).astimezone(zone)
        self.assertEqual((local.day, local.hour, local.minute), (17, 9, 0))

    def test_tonight_and_this_afternoon_are_understood(self):
        zone = ZoneInfo("Asia/Shanghai")
        fixed = datetime(2026, 9, 16, 9, 0, tzinfo=zone).astimezone(timezone.utc)
        tonight = AlertScheduler.parse_when("tonight", now=fixed).astimezone(zone)
        afternoon = AlertScheduler.parse_when("this afternoon", now=fixed).astimezone(zone)
        self.assertEqual((tonight.day, tonight.hour), (16, 20))
        self.assertEqual((afternoon.day, afternoon.hour), (16, 15))

    def test_a_relative_offset_is_not_affected_by_day_words(self):
        zone = ZoneInfo("Asia/Shanghai")
        fixed = datetime(2026, 9, 16, 15, 0, tzinfo=zone).astimezone(timezone.utc)
        due = AlertScheduler.parse_when("10 minutes", now=fixed)
        self.assertEqual(due, fixed + timedelta(minutes=10))


if __name__ == "__main__":
    unittest.main()


class TrailingLabelTests(unittest.TestCase):
    """The label after the time must not make the time unparseable."""

    def setUp(self):
        self.registry = ToolRegistry.discover(
            services={"settings": None, "alert_scheduler": None})

    def test_a_label_after_the_time_becomes_the_message(self):
        # This exact phrasing failed on the Pi: "rest" was swallowed into the
        # time, so the whole request was rejected as an unparseable time.
        self.assertEqual(self.registry.alarm_request("set a ten minute rest timer"),
                         ("ten minute", "rest"))
        self.assertEqual(self.registry.alarm_request("set a five minute tea timer"),
                         ("five minute", "tea"))
        self.assertEqual(self.registry.alarm_request("set a twenty minute focus timer"),
                         ("twenty minute", "focus"))

    def test_a_plain_request_still_gets_the_default_message(self):
        self.assertEqual(self.registry.alarm_request("set an alarm for 10 minutes"),
                         ("10 minutes", "Your alarm is due."))
        self.assertEqual(self.registry.alarm_request("set a timer for thirty"),
                         ("thirty minutes", "Your alarm is due."))
        self.assertEqual(self.registry.alarm_request("start a timer for twenty five"),
                         ("twenty five minutes", "Your alarm is due."))

    def test_an_explicit_to_phrase_is_untouched(self):
        self.assertEqual(
            self.registry.alarm_request("remind me in 10 minutes to take the trash out"),
            ("10 minutes", "take the trash out"))

    def test_a_real_clock_time_survives_a_trailing_label(self):
        self.assertEqual(self.registry.alarm_request("set a 7:30 pm dinner alarm"),
                         ("7:30 pm", "dinner"))


class RemainingWordingTests(unittest.TestCase):
    """A timer must be checkable the moment it is set, not only when it rings."""

    def test_remaining_is_written_the_way_people_say_it(self):
        self.assertEqual(describe_remaining(45), "45 seconds")
        self.assertEqual(describe_remaining(60), "1 minute")
        self.assertEqual(describe_remaining(600), "10 minutes")
        self.assertEqual(describe_remaining(3600), "1 hour")
        self.assertEqual(describe_remaining(8100), "2 hours 15 minutes")
        self.assertEqual(describe_remaining(172800), "2 days")
        self.assertEqual(describe_remaining(-5), "no time at all")
        self.assertEqual(describe_remaining(None), "an unknown time")

    def test_clock_time_is_given_in_the_configured_zone(self):
        zone = ZoneInfo("Asia/Shanghai")
        due = datetime(2026, 9, 16, 12, 42, tzinfo=timezone.utc)  # 20:42 in Shanghai
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(describe_clock(due, now), "8:42 PM today")

    def test_a_later_day_says_which_day(self):
        due = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)   # 09:00 Shanghai, next day
        now = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(describe_clock(due, now), "9:00 AM tomorrow")


class TimerFeedbackTests(unittest.IsolatedAsyncioTestCase):
    """Setting a timer must confirm it was stored and how long is left."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.alerts = AlertScheduler(lambda _text: True,
                                     path=Path(self.directory.name) / "alerts.json")

    async def test_setting_a_timer_confirms_it_and_counts_down(self):
        result = await AlarmTool(self.alerts).execute(
            {"when": "10 minutes", "message": "rest"})
        self.assertTrue(result.success)
        self.assertTrue(result.data["set"])
        self.assertAlmostEqual(result.data["seconds_remaining"], 600, delta=3)
        self.assertIn("10 minutes from now", result.spoken_text)
        self.assertIn("ringing at", result.spoken_text)
        self.assertTrue(result.data["id"], "no id was returned, so it cannot be cancelled")

    async def test_a_short_one_is_a_timer_and_a_long_one_is_an_alarm(self):
        short = await AlarmTool(self.alerts).execute(
            {"when": "10 minutes", "message": "rest"})
        long_one = await AlarmTool(self.alerts).execute(
            {"when": "5 hours", "message": "bake"})
        self.assertTrue(short.spoken_text.startswith("Timer"))
        self.assertTrue(long_one.spoken_text.startswith("Alarm"))

    async def test_a_bad_time_is_refused_and_nothing_is_stored(self):
        result = await AlarmTool(self.alerts).execute(
            {"when": "whenever", "message": "rest"})
        self.assertFalse(result.success)
        self.assertEqual(self.alerts.rows(), [])

    async def test_listing_shows_the_time_left_on_each_one(self):
        await AlarmTool(self.alerts).execute({"when": "10 minutes", "message": "rest"})
        await AlarmTool(self.alerts).execute({"when": "2 hours", "message": "call mum"})
        result = await ListAlarmsTool(self.alerts).execute({})
        self.assertEqual(len(result.data["alarms"]), 2)
        self.assertIn("10 minutes", result.spoken_text)
        self.assertIn("2 hours", result.spoken_text)
        for row in result.data["alarms"]:
            self.assertGreater(row["seconds_remaining"], 0)
            self.assertTrue(row["time_left"])
            self.assertTrue(row["rings_at"])

    async def test_listing_says_so_when_nothing_is_set(self):
        result = await ListAlarmsTool(self.alerts).execute({})
        self.assertTrue(result.success)
        self.assertIn("Nothing is set", result.spoken_text)

    async def test_cancelling_reports_what_went_and_what_it_had_left(self):
        created = await AlarmTool(self.alerts).execute(
            {"when": "10 minutes", "message": "rest"})
        result = await CancelAlarmTool(self.alerts).execute(
            {"alarm_id": created.data["id"]})
        self.assertTrue(result.success)
        self.assertIn("rest", result.spoken_text)
        self.assertIn("left", result.spoken_text)

    async def test_cancelling_an_unknown_id_is_refused(self):
        result = await CancelAlarmTool(self.alerts).execute({"alarm_id": "zzzzzzzz"})
        self.assertFalse(result.success)

    async def test_an_unavailable_scheduler_says_so(self):
        result = await AlarmTool(None).execute({"when": "10 minutes", "message": "rest"})
        self.assertFalse(result.success)
        self.assertEqual(result.spoken_text, UNAVAILABLE)


class RealtimeSessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """A realtime TTS session must be closed, not just forgotten.

    Dropping the reference left a websocket open on DashScope's side after every
    turn, and a realtime session is billed for as long as it stays open. Speech
    was 99% of the bill as a result.
    """

    def _synthesizer(self):
        from athena.tts.qwen import QwenRealtimeSynthesizer
        synth = QwenRealtimeSynthesizer("key", "qwen3-tts-flash-realtime", "Dolce")
        synth._loop = asyncio.get_running_loop()
        return synth

    async def test_an_empty_clause_never_opens_a_session(self):
        synth = self._synthesizer()
        opened = []

        async def fake_start(_turn):
            opened.append(True)

        synth._start = fake_start
        await synth.send_text(uuid4(), "   ")
        await synth.send_text(uuid4(), "")
        self.assertEqual(opened, [], "a session was opened for text with nothing in it")

    async def test_finishing_a_turn_closes_the_socket(self):
        synth = self._synthesizer()
        closed = []

        class FakeSession:
            def close(self):
                closed.append(True)

        synth._session = FakeSession()
        synth._retire()
        self.assertIsNone(synth._session)
        # The close is delayed so the last audio frames can arrive first.
        await asyncio.sleep(2.0)
        self.assertEqual(closed, [True], "the realtime session was left open")

    async def test_retiring_with_nothing_open_is_harmless(self):
        synth = self._synthesizer()
        synth._retire()
        self.assertIsNone(synth._session)


class SpeechBudgetTests(unittest.TestCase):
    """Speech is what costs money; the text still goes everywhere else."""

    def _budget(self, value=None):
        import os
        from unittest.mock import patch
        from athena.coordinator import speech_budget_from_environment
        env = {} if value is None else {"ATHENA_TTS_MAX_CHARS": value}
        with patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop("ATHENA_TTS_MAX_CHARS", None)
            return speech_budget_from_environment()

    def test_speech_is_not_truncated_by_default(self):
        self.assertEqual(self._budget(), 0)

    def test_the_budget_can_be_configured(self):
        self.assertEqual(self._budget("120"), 120)
        self.assertEqual(self._budget("-5"), 0, "a negative budget means no budget")

    def test_a_broken_value_falls_back_instead_of_crashing(self):
        self.assertEqual(self._budget("lots"), 0)


class QuietHoursTests(unittest.TestCase):
    """Nothing unprompted may be spoken in the middle of the night.

    A finished background job once announced a memory failure at 3am and woke the
    house. Scheduled work has no idea what time it is.
    """

    def _quiet(self, hour, minute, window="22:00-07:00"):
        import os
        from datetime import datetime as real_datetime
        from unittest.mock import patch
        from athena.coordinator import VoiceCoordinator

        class FakeDateTime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 17, hour, minute, tzinfo=tz)

        with patch.dict(os.environ, {"ATHENA_QUIET_HOURS": window}, clear=False):
            with patch("athena.coordinator.datetime", FakeDateTime):
                return VoiceCoordinator.__new__(VoiceCoordinator).quiet_hours()

    def test_the_small_hours_are_quiet(self):
        for hour in (23, 0, 3, 6):
            with self.subTest(hour=hour):
                self.assertTrue(self._quiet(hour, 30), f"{hour}:30 should be quiet")

    def test_the_day_is_not_quiet(self):
        for hour in (7, 10, 16, 21):
            with self.subTest(hour=hour):
                self.assertFalse(self._quiet(hour, 30), f"{hour}:30 should not be quiet")

    def test_the_boundaries_belong_to_the_waking_day(self):
        self.assertFalse(self._quiet(7, 0), "07:00 is the start of the day")
        self.assertTrue(self._quiet(22, 0), "22:00 is the start of the night")

    def test_a_window_that_does_not_cross_midnight_works_too(self):
        self.assertTrue(self._quiet(13, 0, window="12:00-14:00"))
        self.assertFalse(self._quiet(15, 0, window="12:00-14:00"))

    def test_a_broken_window_never_silences_athena(self):
        self.assertFalse(self._quiet(3, 0, window="nonsense"))
        self.assertFalse(self._quiet(3, 0, window="22:00-22:00"))
