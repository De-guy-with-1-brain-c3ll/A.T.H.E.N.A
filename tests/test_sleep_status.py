"""Sleep consolidation: fast enough to be worth running, and checkable while it runs.

Two complaints drove this file.

The pass took fifteen seconds to read a day and had no way to say what it was
doing: a background job with no observable state is indistinguishable from one
that died. And "is your memory up to date" had no true answer at all, so the model
improvised one.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from athena.memory.database import MemoryDatabase, StoredTurn
from athena.sleep import (
    CHUNK_CHARS,
    MODEL_CALL_TIMEOUT_SECONDS,
    SleepBusy,
    SleepCycle,
    SleepRunner,
    SleepStatus,
    SleepStatusStore,
    chunk_turns,
    last_consolidated_date,
    merge_results,
    pending_days,
    sleep_status,
    status_report,
)
from athena.tools.sleep import SleepModeTool, SleepStatusTool


class _Completions:
    def __init__(self, payload, fail_times=0, hang=False):
        self.payload = payload
        self.fail_times = fail_times
        self.hang = hang
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("model unavailable")
        payload = self.payload
        if callable(payload):
            # The prompt is passed in so a fake can decide per part rather than
            # per call: a part that fails must fail on the retry too, or the test
            # measures the retry instead of the failure.
            payload = payload(kwargs["messages"][1]["content"])
        return NS(choices=[NS(message=NS(content=json.dumps(payload)))])


class _Client:
    def __init__(self, payload, fail_times=0, hang=False):
        self.completions = _Completions(payload, fail_times, hang)
        self.chat = NS(completions=self.completions)

    async def close(self):
        pass


class _Harness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # ignore_cleanup_errors: on Windows a SQLite connection that has merely
        # been dropped still holds the file until the collector runs, and deleting
        # the directory in the meantime raises PermissionError. That is a teardown
        # race, not a test result, and it must not turn a passing test red.
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = MemoryDatabase(self.root / "athena.db")
        self.database.initialize()
        self.status = SleepStatusStore(self.root / "sleep-status.json")
        # Every reader goes through the real lookup, pointed at this test's files,
        # so the test exercises the same path production uses.
        for target in ("athena.sleep", "athena.tools.sleep"):
            patcher = patch(f"{target}.SleepStatusStore", lambda *a, **k: self.status)
            patcher.start()
            self.addCleanup(patcher.stop)
        db_patcher = patch("athena.sleep.database_path", lambda: self.database.path)
        db_patcher.start()
        self.addCleanup(db_patcher.stop)

    def _turns(self, count=2, text=None):
        for index in range(count):
            self.database.save_turn(StoredTurn(
                uuid4(), text or f"question {index}", text or f"answer {index}"))

    def _cycle(self, payload, status=None, **kwargs):
        client = _Client(payload, **kwargs)
        with patch("athena.sleep.AsyncOpenAI", lambda **rest: client):
            cycle = SleepCycle(self.database, "test-key",
                               status=status or self.status)
        cycle._client = client
        return cycle

    async def _run(self, day=None, **kwargs):
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []}, **kwargs)
        return await cycle.run(day)


class ChunkingTests(unittest.TestCase):
    def _turn(self, size):
        return NS(user_text="u" * size, assistant_text="a" * size)

    def test_a_short_day_is_one_request(self):
        turns = [self._turn(50) for _ in range(5)]
        self.assertEqual(len(chunk_turns(turns)), 1)

    def test_a_long_day_is_split_without_splitting_a_turn(self):
        turns = [self._turn(1000) for _ in range(20)]
        chunks = chunk_turns(turns)
        self.assertGreater(len(chunks), 1)
        # Every turn appears exactly once as a *new* turn, in order.
        rebuilt = [turn for chunk in chunks for turn in chunk]
        self.assertEqual(len(rebuilt), len(turns) + (len(chunks) - 1) * 1)
        for chunk in chunks:
            self.assertTrue(all(turn in turns for turn in chunk))

    def test_neighbouring_chunks_overlap_so_a_decision_is_visible_in_both(self):
        turns = [self._turn(1000) for _ in range(20)]
        chunks = chunk_turns(turns)
        self.assertEqual(chunks[0][-1], chunks[1][0])

    def test_the_size_limit_is_respected(self):
        turns = [self._turn(1000) for _ in range(20)]
        chunks = chunk_turns(turns, limit=6000)
        self.assertGreater(len(chunks), 1)
        # Every chunk but the last must be full, so the day is actually divided
        # rather than one chunk quietly doing all the work.
        for chunk in chunks[:-1]:
            size = sum(len(t.user_text) + len(t.assistant_text) + 40 for t in chunk)
            self.assertGreater(size, 4000)

    def test_an_empty_day_chunks_to_nothing(self):
        self.assertEqual(chunk_turns([]), [])


class MergeTests(unittest.TestCase):
    def test_facts_from_every_chunk_survive(self):
        merged = merge_results([
            {"summary": "first.", "facts": [{"key": "a", "value": "1", "confidence": 0.9}],
             "forget_keys": []},
            {"summary": "second.", "facts": [{"key": "b", "value": "2", "confidence": 0.9}],
             "forget_keys": []},
        ])
        self.assertEqual({fact["key"] for fact in merged["facts"]}, {"a", "b"})
        self.assertIn("first.", merged["summary"])
        self.assertIn("second.", merged["summary"])

    def test_a_later_chunk_wins_for_the_same_key(self):
        merged = merge_results([
            {"summary": "s", "facts": [{"key": "a", "value": "old", "confidence": 0.9}],
             "forget_keys": []},
            {"summary": "s", "facts": [{"key": "a", "value": "new", "confidence": 0.9}],
             "forget_keys": []},
        ])
        self.assertEqual(merged["facts"], [{"key": "a", "value": "new", "confidence": 0.9}])

    def test_an_identical_summary_is_not_repeated_once_per_chunk(self):
        merged = merge_results([
            {"summary": "same words.", "facts": [], "forget_keys": []},
            {"summary": "same words.", "facts": [], "forget_keys": []},
            {"summary": "same words.", "facts": [], "forget_keys": []},
        ])
        self.assertEqual(merged["summary"], "same words.")

    def test_a_forgotten_key_is_not_resurrected_by_an_earlier_chunk(self):
        merged = merge_results([
            {"summary": "s", "facts": [{"key": "a", "value": "1", "confidence": 0.9}],
             "forget_keys": []},
            {"summary": "s", "facts": [], "forget_keys": ["a"]},
        ])
        self.assertEqual(merged["facts"], [])
        self.assertEqual(merged["forget_keys"], ["a"])


class SpeedTests(_Harness):
    async def test_a_long_day_costs_more_than_one_request(self):
        self._turns(15, text="x" * 900)
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []})
        report = await cycle.run()
        self.assertGreater(report.chunks, 1)
        self.assertEqual(report.requests, report.chunks)
        self.assertEqual(len(cycle._client.completions.calls), report.chunks)

    async def test_a_short_day_still_costs_exactly_one_request(self):
        self._turns(3, text="small")
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []})
        report = await cycle.run()
        self.assertEqual(report.chunks, 1)
        self.assertEqual(report.requests, 1)

    async def test_the_day_is_never_sent_in_one_enormous_prompt(self):
        self._turns(20, text="y" * 1200)
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []})
        await cycle.run()
        for call in cycle._client.completions.calls:
            self.assertLess(len(call["messages"][1]["content"]), CHUNK_CHARS + 6000)

    async def test_one_unreadable_part_does_not_discard_the_whole_day(self):
        self._turns(15, text="z" * 900)

        def payload(prompt):
            if "part 2 of" in prompt:
                raise RuntimeError("part two is unreadable")
            return {"summary": "kept", "facts": [], "forget_keys": []}

        cycle = self._cycle(payload)
        report = await cycle.run()
        self.assertTrue(report.partial)
        self.assertEqual(len(report.failures), 1)
        self.assertIn("part 2", report.failures[0])
        # The good parts still landed, and the failure is readable in the report.
        self.assertEqual(self.database.get_summary(), "kept")
        self.assertIn("WARNING", report.describe())

    async def test_a_partial_pass_still_reports_what_it_did_read(self):
        self._turns(15, text="z" * 900)

        def payload(prompt):
            if "part 2 of" in prompt:
                raise RuntimeError("part two is unreadable")
            return {"summary": "kept", "facts": [], "forget_keys": []}

        cycle = self._cycle(payload)
        report = await cycle.run()
        self.assertEqual(report.requests, report.chunks - 1)
        self.assertEqual(report.turns, 15)
        self.assertIn("short-term read", report.describe())

    async def test_a_partial_pass_is_not_recorded_as_a_complete_day(self):
        self._turns(15, text="z" * 900)

        def payload(prompt):
            if "part 2 of" in prompt:
                raise RuntimeError("nope")
            return {"summary": "kept", "facts": [], "forget_keys": []}

        cycle = self._cycle(payload)
        report = await cycle.run()
        self.assertTrue(report.partial)
        # Claiming the day is done would hide the turns that were never read.
        self.assertIsNone(last_consolidated_date())

    async def test_a_pass_that_read_nothing_at_all_fails_loudly(self):
        self._turns(15, text="z" * 900)

        def payload(prompt):
            raise RuntimeError("the provider is down")

        cycle = self._cycle(payload)
        with self.assertRaises(Exception):
            await cycle.run()
        self.assertEqual(self.status.load().phase, "failed")

    async def test_a_model_that_stalls_is_bounded(self):
        self._turns(1)
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []}, hang=True)
        with patch("athena.sleep.MODEL_CALL_TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(Exception):
                await cycle.run()
        # And the status says it failed rather than looking stuck forever.
        self.assertEqual(self.status.load().phase, "failed")

    async def test_the_model_call_has_a_declared_ceiling(self):
        self.assertGreater(MODEL_CALL_TIMEOUT_SECONDS, 0)
        self.assertLessEqual(MODEL_CALL_TIMEOUT_SECONDS, 120)


class StatusTests(_Harness):
    async def test_a_finished_pass_is_described_afterwards(self):
        self._turns(3)
        await self._run()
        status = self.status.load()
        self.assertEqual(status.phase, "done")
        self.assertEqual(status.turns, 3)
        self.assertIn("Last consolidated", status.describe())

    async def test_the_status_records_what_the_pass_produced(self):
        self._turns(2)
        cycle = self._cycle({"summary": "a summary", "facts": [
            {"key": "history_interest", "value": "Mongol trade routes", "confidence": 0.9}],
            "forget_keys": []})
        await cycle.run()
        status = self.status.load()
        self.assertEqual(status.facts_written, 1)
        self.assertEqual(status.summary_chars, len("a summary"))
        # The clock must always be recorded, however fast the fake provider is:
        # "seconds" is how a slow pass is told apart from a hung one.
        self.assertIsNotNone(status.seconds)
        self.assertGreaterEqual(status.seconds, 0)

    async def test_a_day_with_nothing_in_it_says_so_instead_of_claiming_success(self):
        report = await self._run(date(2020, 1, 1))
        status = self.status.load()
        self.assertEqual(status.phase, "skipped")
        self.assertIn("no conversation", report.skipped)
        self.assertIn("nothing to consolidate", status.describe())

    async def test_a_failure_is_recorded_rather_than_left_running(self):
        self._turns(1)

        def boom(_):
            raise RuntimeError("the provider is down")

        cycle = self._cycle(boom)
        with self.assertRaises(Exception):
            await cycle.run()
        status = self.status.load()
        self.assertEqual(status.phase, "failed")
        self.assertIn("provider is down", status.describe())

    async def test_a_second_pass_is_refused_while_one_is_live(self):
        self._turns(2)
        other = SleepStatus(
            phase="thinking", day="2026-09-16", pid=os.getpid() + 1,
            heartbeat_at=datetime.now(timezone.utc).isoformat())
        self.status._write(other)
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []})
        with self.assertRaises(SleepBusy) as caught:
            await cycle.run()
        self.assertEqual(caught.exception.record["phase"], "thinking")

    async def test_a_stale_record_from_a_dead_pass_does_not_block_forever(self):
        self._turns(2)
        dead = SleepStatus(
            phase="thinking", day="2026-09-16", pid=os.getpid() + 1,
            heartbeat_at=(datetime.now(timezone.utc) - timedelta(hours=4)).isoformat())
        self.status._write(dead)
        report = await self._run()
        self.assertIsNone(report.skipped)
        self.assertEqual(self.status.load().phase, "done")

    async def test_the_pass_records_which_day_it_covered(self):
        self._turns(1)
        await self._run()
        self.assertEqual(last_consolidated_date(), datetime.now().date())

    async def test_a_dry_run_does_not_claim_the_day_was_consolidated(self):
        self._turns(1)
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []})
        await cycle.run(dry_run=True)
        self.assertIsNone(last_consolidated_date())

    def test_no_record_at_all_reads_as_never(self):
        self.assertEqual(sleep_status().phase, "idle")
        self.assertIn("never", sleep_status().describe())

    def test_a_corrupt_record_does_not_break_the_reader(self):
        (self.root / "sleep-status.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.status.load().phase, "idle")

    def test_an_unknown_field_from_a_newer_version_is_ignored(self):
        (self.root / "sleep-status.json").write_text(
            json.dumps({"phase": "done", "day": "2026-09-16", "future_field": 1}),
            encoding="utf-8")
        self.assertEqual(self.status.load().phase, "done")

    def test_the_dashboard_line_is_one_line(self):
        self.status._write(SleepStatus(
            phase="done", day="2026-09-16", turns=12, facts_written=3, seconds=4.5))
        self.assertNotIn("\n", self.status.load().summary_line())

    def test_a_running_pass_reports_progress_not_completion(self):
        self.status._write(SleepStatus(
            phase="thinking", day="2026-09-16", chunks=3, requests=1,
            heartbeat_at=datetime.now(timezone.utc).isoformat()))
        text = self.status.load().describe()
        self.assertIn("right now", text)
        self.assertIn("part 2 of 3", text)


class PendingDaysTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "athena.db"
        store = MemoryDatabase(self.path)
        store.initialize()
        self.store = store

    def _set_last(self, day):
        if day:
            self.store.record_consolidation(day.isoformat())

    def test_days_after_the_last_pass_are_waiting(self):
        self._set_last(date(2026, 9, 12))
        with patch("athena.sleep.database_path", lambda: self.path):
            waiting = pending_days(datetime(2026, 9, 15, 23, 0))
        self.assertEqual(waiting, ["2026-09-13", "2026-09-14", "2026-09-15"])

    def test_today_is_not_counted_as_behind_during_the_day(self):
        self._set_last(date(2026, 9, 14))
        with patch("athena.sleep.database_path", lambda: self.path), \
             patch("athena.sleep.now_local", lambda: datetime(2026, 9, 15, 14, 0)):
            waiting = pending_days()
        self.assertEqual(waiting, [])

    def test_nothing_is_waiting_once_the_last_pass_covers_today(self):
        self._set_last(date(2026, 9, 15))
        with patch("athena.sleep.database_path", lambda: self.path):
            self.assertEqual(pending_days(datetime(2026, 9, 15, 23, 0)), [])

    def test_the_report_names_the_days_that_are_waiting(self):
        store = SleepStatusStore(self.root / "sleep-status.json")
        store._write(SleepStatus(phase="done", day="2026-09-14", turns=4,
                                 facts_written=1, seconds=2.0))
        with patch("athena.sleep.database_path", lambda: self.path), \
             patch("athena.sleep.SleepStatusStore", lambda: store), \
             patch("athena.sleep.now_local", lambda: datetime(2026, 9, 15, 23, 0)):
            text = status_report()
        self.assertIn("waiting on:", text)
        self.assertIn("2026-09-15", text)


class SleepToolTests(_Harness):
    async def test_the_status_tool_reports_a_completed_pass(self):
        self._turns(4)
        await self._run()
        result = await SleepStatusTool().execute({})
        self.assertTrue(result.success)
        self.assertIn("Last consolidated", result.spoken_text)
        self.assertEqual(result.data["turns"], 4)

    async def test_the_status_tool_falls_back_to_the_database_not_to_never(self):
        self.database.record_consolidation("2026-09-15")
        result = await SleepStatusTool().execute({})
        # A lost status file must not make ATHENA claim it has never consolidated.
        self.assertIn("2026-09-15", result.spoken_text)

    async def test_the_status_tool_is_truthful_when_nothing_has_run(self):
        result = await SleepStatusTool().execute({})
        self.assertIn("not consolidated my memory yet", result.spoken_text)

    async def test_the_mode_tool_delegates_to_a_running_coordinator(self):
        calls = []

        class Coordinator:
            async def start_sleep(self, day=None):
                calls.append(day)
                return NS(success=True, spoken_text="Going to sleep.")

        tool = SleepModeTool(coordinator=Coordinator())
        result = await tool.execute({})
        self.assertTrue(result.success)
        self.assertEqual(calls, [None])
        self.assertIn("Going to sleep", result.spoken_text)

    async def test_the_mode_tool_parses_a_day_for_the_coordinator(self):
        calls = []

        class Coordinator:
            async def start_sleep(self, day=None):
                calls.append(day)
                return NS(success=True, spoken_text="ok")

        await SleepModeTool(coordinator=Coordinator()).execute({"day": "2026-09-15"})
        self.assertEqual(calls, [date(2026, 9, 15)])

    async def test_the_mode_tool_rejects_a_bad_date_without_starting_anything(self):
        result = await SleepModeTool(coordinator=object()).execute({"day": "yesterday"})
        self.assertFalse(result.success)
        self.assertIn("is not a date", result.spoken_text)

    async def test_the_mode_tool_runs_inline_when_nothing_is_coordinating(self):
        self._turns(2)
        runner = SleepRunner(database=self.database.path, api_key="k", status=self.status)
        client = _Client({"summary": "inline", "facts": [], "forget_keys": []})
        with patch("athena.sleep.AsyncOpenAI", lambda **rest: client):
            result = await SleepModeTool(status=self.status, runner=runner).execute({})
        # No start_sleep on the coordinator, so the tool ran the pass and can report it.
        self.assertTrue(result.success)
        self.assertIn("Consolidated", result.spoken_text)

    async def test_the_mode_tool_accepts_the_runner_object_not_just_its_method(self):
        # bind() hands over the SleepRunner itself, whose consolidate() takes
        # dry_run as keyword-only. Passing the object used to raise TypeError and
        # surface as "Sleep mode could not run" on every non-voice interface.
        self._turns(2)
        runner = SleepRunner(database=self.database.path, api_key="k", status=self.status)
        client = _Client({"summary": "inline", "facts": [], "forget_keys": []})
        with patch("athena.sleep.AsyncOpenAI", lambda **rest: client):
            result = await SleepModeTool(status=self.status, runner=runner).execute({})
        self.assertTrue(result.success, result.spoken_text)
        self.assertNotIn("could not run", result.spoken_text)
        self.assertIn("Consolidated", result.spoken_text)

    async def test_a_runner_without_a_key_says_so_rather_than_crashing(self):
        runner = SleepRunner(database=self.database.path, api_key=None, status=self.status)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            result = await SleepModeTool(status=self.status, runner=runner).execute({})
        self.assertFalse(result.success)
        self.assertIn("DEEPSEEK_API_KEY", result.spoken_text)

    async def test_the_mode_tool_refuses_a_second_parallel_pass(self):
        self.status._write(SleepStatus(
            phase="thinking", day="2026-09-16", pid=os.getpid() + 1,
            heartbeat_at=datetime.now(timezone.utc).isoformat()))
        result = await SleepModeTool(status=self.status).execute({})
        self.assertFalse(result.success)
        self.assertIn("Already consolidating", result.spoken_text)

    async def test_the_mode_tool_hands_the_job_to_the_running_coordinator(self):
        calls = []

        class Coordinator:
            async def start_sleep(self, day=None):
                calls.append(day)
                return NS(success=True, spoken_text="Going to sleep.")

        result = await SleepModeTool(coordinator=Coordinator()).execute({})
        # The coordinator owns the background path, so the tool must not also run
        # its own pass: two passes over one day is wasted money.
        self.assertTrue(result.success)
        self.assertEqual(calls, [None])


class RunnerTests(_Harness):
    async def test_the_runner_reports_another_process_is_running(self):
        runner = SleepRunner(database=self.database.path, api_key="k", status=self.status)
        self.assertFalse(runner.is_running())
        self.status._write(SleepStatus(
            phase="thinking", pid=os.getpid() + 1,
            heartbeat_at=datetime.now(timezone.utc).isoformat()))
        self.assertTrue(runner.is_running())

    async def test_a_finished_pass_is_not_still_running(self):
        runner = SleepRunner(database=self.database.path, api_key="k", status=self.status)
        self.status._write(SleepStatus(
            phase="done", pid=os.getpid() + 1,
            heartbeat_at=datetime.now(timezone.utc).isoformat()))
        self.assertFalse(runner.is_running())

    async def test_a_missing_key_is_an_error_not_a_silent_no_op(self):
        runner = SleepRunner(database=self.database.path, api_key="", status=self.status)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                await runner.consolidate()


class MessageShapeTests(_Harness):
    async def test_later_parts_are_told_earlier_parts_already_landed(self):
        self._turns(15, text="q" * 900)
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []})
        await cycle.run()
        second = cycle._client.completions.calls[1]["messages"][1]["content"]
        self.assertIn("part 2 of", second)
        self.assertIn("already been", second)

    async def test_the_first_part_is_labelled_as_the_whole_day(self):
        self._turns(2)
        cycle = self._cycle({"summary": "s", "facts": [], "forget_keys": []})
        await cycle.run()
        first = cycle._client.completions.calls[0]["messages"][1]["content"]
        self.assertIn("A FULL DAY OF CONVERSATION", first)


class AppliedReportingTests(_Harness):
    async def test_a_refused_fact_is_counted_not_silently_dropped(self):
        self._turns(1)
        cycle = self._cycle({
            "summary": "s",
            "facts": [{"key": "timer_count", "value": "3 timers", "confidence": 0.9}],
            "forget_keys": [],
        })
        report = await cycle.run()
        # The quality gate refuses a fact about a specific timer. Saying "0 facts"
        # without saying "and I refused one" hides the reason from him.
        self.assertEqual(report.facts_written, 0)
        self.assertEqual(report.offered_refused, 1)
        self.assertIn("refused", report.describe())
