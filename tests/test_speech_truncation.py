"""Speech must never be cut off by a clock meant for synthesis.

The same mistake was made twice: the playback was awaited inside the 30 second
synthesis timeout, so any reply longer than half a minute stopped mid-sentence
while the full text sat in the log. A channel roundup is well over that.
"""
import asyncio
from types import SimpleNamespace
import unittest

from athena.coordinator import VoiceCoordinator
from athena.events import AudioChunk
from athena.state import AgentState


BYTES_PER_SECOND = 48_000          # 24 kHz, 16-bit mono


class FakeTts:
    def __init__(self, seconds: float, realtime: bool = False):
        self.seconds = seconds
        self.realtime = realtime
        self.cancelled = False

    async def send_text(self, turn, text):
        pass

    async def flush(self, turn):
        pass

    async def cancel(self, turn):
        self.cancelled = True

    async def audio(self, turn):
        remaining = int(self.seconds * BYTES_PER_SECOND)
        while remaining > 0:
            size = min(BYTES_PER_SECOND, remaining)
            yield AudioChunk(turn, b"\x00" * size)
            remaining -= size
            if self.realtime:
                # One second of audio per second, the way a speaker behaves.
                await asyncio.sleep(0.02)


class FakeSpeaker:
    def __init__(self):
        self.bytes = 0

    async def play(self, pcm):
        self.bytes += len(pcm)


def coordinator_with(tts, synth_timeout: float = 1.0):
    import os
    from unittest.mock import patch
    patcher = patch.dict(os.environ, {"ATHENA_TTS_SYNTH_TIMEOUT": str(synth_timeout)})
    patcher.start()
    coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
    coordinator.tts = tts
    coordinator.speaker = FakeSpeaker()
    coordinator.state = AgentState.IDLE
    coordinator.wake_word = False
    coordinator._active_until = 0
    coordinator.llm = SimpleNamespace(_tools={})
    from collections import OrderedDict
    coordinator._speech_cache = OrderedDict()
    return coordinator


class LongSpeechIsNotTruncatedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_roundup_longer_than_the_synthesis_clock_is_played_in_full(self):
        """Forty seconds of speech through the background-result path."""
        tts = FakeTts(40.0, realtime=True)
        coordinator = coordinator_with(tts)
        await coordinator._speak_text("a channel roundup, well over half a minute")
        self.assertEqual(
            coordinator.speaker.bytes, int(40.0 * BYTES_PER_SECOND),
            "playback stopped early: it is still sharing the synthesis clock")

    async def test_a_very_long_answer_is_still_played_in_full(self):
        tts = FakeTts(75.0, realtime=True)
        coordinator = coordinator_with(tts)
        await coordinator._speak_text("a long briefing")
        self.assertEqual(coordinator.speaker.bytes, int(75.0 * BYTES_PER_SECOND))

    async def test_a_short_answer_is_unaffected(self):
        tts = FakeTts(3.0)
        coordinator = coordinator_with(tts)
        await coordinator._speak_text("short")
        self.assertEqual(coordinator.speaker.bytes, int(3.0 * BYTES_PER_SECOND))

    async def test_the_session_is_released_after_speaking(self):
        tts = FakeTts(2.0)
        coordinator = coordinator_with(tts)
        await coordinator._speak_text("short")
        self.assertTrue(tts.cancelled, "the synthesis session was not released")


class PlaybackReportsWhatItPlayedTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_counts_the_bytes_it_hands_to_the_speaker(self):
        tts = FakeTts(5.0)
        coordinator = coordinator_with(tts)
        turn = __import__("uuid").uuid4()
        coordinator.active_turn = turn
        played = await coordinator._play_audio(turn)
        self.assertEqual(played, int(5.0 * BYTES_PER_SECOND))

    async def test_audio_from_a_superseded_turn_is_dropped_and_said_so(self):
        tts = FakeTts(5.0)
        coordinator = coordinator_with(tts)
        turn = __import__("uuid").uuid4()
        coordinator.active_turn = __import__("uuid").uuid4()   # a newer turn
        played = await coordinator._play_audio(turn)
        self.assertEqual(played, 0, "audio from an old turn should not be played")


class RepeatedSpeechIsFreeTests(unittest.IsolatedAsyncioTestCase):
    """Synthesis is the expensive part; the same words must not be paid for twice."""

    async def test_the_second_identical_reply_is_replayed_not_synthesised(self):
        tts = FakeTts(2.0)
        coordinator = coordinator_with(tts)
        text = "There are no posts in that channel yet."

        await coordinator._speak_text(text)
        first = coordinator.speaker.bytes
        self.assertGreater(first, 0)
        self.assertIn(text, coordinator._speech_cache)

        # Swap in a synthesizer that refuses to be used, so a cache miss is loud.
        class Refuses:
            async def send_text(self, turn, value):
                raise AssertionError("the reply was synthesised a second time")

            async def flush(self, turn):
                raise AssertionError("the reply was synthesised a second time")

            async def cancel(self, turn):
                pass

            async def audio(self, turn):
                raise AssertionError("the reply was synthesised a second time")
                yield

        coordinator.tts = Refuses()
        await coordinator._speak_text(text)
        self.assertEqual(coordinator.speaker.bytes, first * 2,
                         "the repeat was not replayed from the cache")

    async def test_a_different_reply_is_still_synthesised(self):
        tts = FakeTts(2.0)
        coordinator = coordinator_with(tts)
        await coordinator._speak_text("first reply")
        await coordinator._speak_text("a completely different reply")
        self.assertEqual(coordinator.speaker.bytes, int(2.0 * BYTES_PER_SECOND) * 2)

    async def test_the_cache_does_not_grow_without_bound(self):
        from athena.coordinator import SPEECH_CACHE_ENTRIES
        tts = FakeTts(0.1)
        coordinator = coordinator_with(tts)
        for index in range(SPEECH_CACHE_ENTRIES + 10):
            await coordinator._speak_text(f"reply number {index} with enough characters")
        self.assertLessEqual(len(coordinator._speech_cache), SPEECH_CACHE_ENTRIES)

    async def test_a_short_fragment_is_not_cached(self):
        tts = FakeTts(0.1)
        coordinator = coordinator_with(tts)
        await coordinator._speak_text("ok")
        self.assertEqual(len(coordinator._speech_cache), 0)


class HandsFreeWindowTests(unittest.IsolatedAsyncioTestCase):
    """Only speech the user asked for may reopen the hands-free window.

    A watcher announcement used to re-arm it, so for twenty seconds afterwards
    any conversation in the room was taken as a command and answered.
    """

    async def _speak(self, prompted):
        import time
        tts = FakeTts(0.2)
        coordinator = coordinator_with(tts)
        coordinator.wake_word = "athena"
        coordinator._active_until = time.monotonic() - 1     # window already shut
        await coordinator._speak_text("a watcher announcement", prompted=prompted)
        return coordinator._active_until

    async def test_an_unprompted_announcement_does_not_reopen_the_window(self):
        import time
        before = time.monotonic()
        after = await self._speak(prompted=False)
        self.assertLess(after, before, "an announcement reopened the hands-free window")

    async def test_a_reply_the_user_asked_for_does_reopen_it(self):
        import time
        before = time.monotonic()
        after = await self._speak(prompted=True)
        self.assertGreater(after, before + 10, "the follow-up window was not extended")
