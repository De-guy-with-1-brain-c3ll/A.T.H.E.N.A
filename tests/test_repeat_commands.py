"""Repeated commands: one answer, one acknowledgement, not two answers.

The user repeats himself when the first answer feels slow. Answering the
repeat again sounded like a malfunction; a single quick acknowledgement (no
model call) tells him he was heard, and the original answer is already on its
way. Insisting a third time within the window is honoured for real.
"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from athena.coordinator import VoiceCoordinator


def loop_coordinator(transcripts, submit_result=object()):
    """A VoiceCoordinator with just enough real state to run() the main loop."""
    coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
    coordinator.llm = SimpleNamespace(shutdown_requested=False)
    coordinator._external_speech = asyncio.Queue()
    coordinator._external_changed = asyncio.Event()
    coordinator.background = MagicMock()
    coordinator.background.changed = asyncio.Event()
    coordinator.background.next_output.return_value = None
    coordinator.background.submit.return_value = submit_result
    coordinator.background.jobs = {}
    coordinator.voice_gate = MagicMock()
    coordinator.speaker = MagicMock()
    coordinator.memory = MagicMock()
    coordinator.memory.context_messages.return_value = []
    coordinator._ack_pcm = b""
    coordinator._alarm_pcm = b""
    coordinator._sleep_task = None
    coordinator._listen_task = None
    coordinator.wake_word = ""
    coordinator._active_until = 0.0
    coordinator._brief_offer = None
    coordinator._last_command_text = None
    coordinator._last_command_at = 0.0
    coordinator._repeat_acknowledged = False
    coordinator._speak_text = AsyncMock()
    coordinator.background.cancel_all = AsyncMock()

    index = {"n": 0}

    async def listen(turn):
        text = transcripts[min(index["n"], len(transcripts) - 1)]
        index["n"] += 1
        if index["n"] >= len(transcripts):
            coordinator.llm.shutdown_requested = True
        return text

    coordinator._listen = listen
    return coordinator


class RepeatKeyTests(unittest.TestCase):
    """STT punctuation and casing drift between runs; repeats must still match."""

    def test_case_punctuation_and_spacing_are_ignored(self):
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        self.assertEqual(coordinator._repeat_key("What's the weather?"),
                         coordinator._repeat_key("  whats THE weather "))

    def test_different_words_are_different_commands(self):
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        self.assertNotEqual(coordinator._repeat_key("what's the weather"),
                            coordinator._repeat_key("whats the time"))


class RepeatWindowTests(unittest.TestCase):
    def _coordinator(self):
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        coordinator._last_command_text = None
        coordinator._last_command_at = 0.0
        coordinator._repeat_acknowledged = False
        return coordinator

    def test_the_first_saying_is_never_a_repeat(self):
        coordinator = self._coordinator()
        self.assertFalse(coordinator._is_repeat("what's the weather"))

    def test_an_immediate_repeat_is_caught_once(self):
        coordinator = self._coordinator()
        coordinator._remember_command("what's the weather")
        self.assertTrue(coordinator._is_repeat("What's the weather!"))
        # The next saying is honoured for real: insisting means it.
        self.assertFalse(coordinator._is_repeat("what's the weather"))

    def test_a_different_command_resets_the_tracking(self):
        coordinator = self._coordinator()
        coordinator._remember_command("what's the weather")
        self.assertFalse(coordinator._is_repeat("set a timer for ten minutes"))
        self.assertFalse(coordinator._repeat_acknowledged)

    def test_an_old_repeat_is_a_fresh_command(self):
        coordinator = self._coordinator()
        coordinator._remember_command("what's the weather")
        coordinator._last_command_at = time.monotonic() - 31.0
        self.assertFalse(coordinator._is_repeat("what's the weather"))


class RepeatLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_loop_answers_once_and_acknowledges_the_repeat(self):
        coordinator = loop_coordinator(["what's the weather"] * 3)
        await coordinator.run()
        # First and third sayings were submitted; the middle one was acknowledged.
        self.assertEqual(coordinator.background.submit.call_count, 2)
        self.assertEqual(coordinator._speak_text.await_count, 1)
        self.assertIn(coordinator._speak_text.await_args.args[0],
                      VoiceCoordinator.REPEAT_ACKS)

    async def test_distinct_commands_are_all_submitted(self):
        coordinator = loop_coordinator(["what's the weather",
                                        "set a timer for ten minutes"])
        await coordinator.run()
        self.assertEqual(coordinator.background.submit.call_count, 2)
        coordinator._speak_text.assert_not_awaited()

    async def test_a_full_queue_still_speaks_the_pending_warning(self):
        coordinator = loop_coordinator(["what's the weather"], submit_result=None)
        await coordinator.run()
        self.assertEqual(coordinator._speak_text.await_count, 1)
        self.assertIn("pending", coordinator._speak_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
