"""Speech must survive intact from the moment it starts to the moment it stops.

Retention is the whole contract of the listening path: the voice gate decides
where a turn opens and closes, and every frame between those two points has to
reach the recogniser, in order, exactly once. These tests are the evidence for
that claim. They also pin down the only bound that exists — the pre-roll window
— and prove it can never discard speech, only silence older than the window.
"""
from array import array
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from athena.audio.telemetry import AudioStatusWriter
from athena.audio.vad import VoiceGate
from athena.coordinator import VoiceCoordinator, end_of_speech_tone
from athena.events import Transcript
from athena.remote_audio import (
    MAX_BACKLOG_FRAMES,
    MICROPHONE_RATE,
    BrowserMicrophone,
    RemoteAudio,
)


FRAME_SAMPLES = 320
SAMPLE_RATE = 24_000


def frame(amplitude: int) -> bytes:
    return array("h", [amplitude] * FRAME_SAMPLES).tobytes()


def unique_script(kinds):
    """A frame per kind, every one distinguishable from all the others.

    Each amplitude stays inside its band — quiet below the start threshold,
    soft between the release and the start threshold, loud above it — while a
    small per-frame offset makes each frame's bytes unique. Without that, two
    identical frames make a reordering or a duplicate impossible to see.
    """
    seen = {"quiet": 0, "soft": 0, "loud": 0, "silence": 0}
    amplitudes = []
    for kind in kinds:
        seen[kind] += 1
        if kind == "quiet":
            amplitudes.append(30 + seen[kind])
        elif kind == "soft":
            amplitudes.append(340 + seen[kind] * 2)
        elif kind == "loud":
            amplitudes.append(700 + seen[kind] * 3)
        else:
            amplitudes.append(1 + seen[kind])
    return amplitudes


def utterance():
    return ["quiet"] * 5 + ["loud"] * 20 + ["silence"] * 12


def opening_gate() -> VoiceGate:
    return VoiceGate(
        minimum_rms=500,
        noise_multiplier=2.0,
        start_ms=100,
        minimum_speech_ms=180,
        end_silence_ms=220,
        pre_roll_ms=800,
    )


def walk(amplitudes):
    """Run one utterance through a fresh gate and report the whole turn.

    Returns the frames, what the gate emitted, and the index of the frame that
    closed the turn. `emitted` is exactly what the recogniser would receive.
    """
    gate = opening_gate()
    frames = [frame(amplitude) for amplitude in amplitudes]
    emitted = []
    for index, pcm in enumerate(frames):
        emitted.extend(gate.process(pcm))
        if gate.should_end:
            return gate, frames, emitted, index
    return gate, frames, emitted, None


class GateRetentionTests(unittest.TestCase):
    def test_every_frame_from_onset_to_end_of_speech_arrives_once_in_order(self):
        amplitudes = unique_script(utterance())
        gate, frames, emitted, end_index = walk(amplitudes)

        self.assertIsNotNone(end_index, "the gate never closed the turn")
        self.assertEqual(
            emitted, frames[:end_index + 1],
            "frames were lost, reordered or duplicated between onset and end of speech")

    def test_the_first_quiet_frame_before_speech_is_still_retained(self):
        """The onset is detected after the fact, so the run-up must be kept."""
        amplitudes = unique_script(utterance())
        gate, frames, emitted, end_index = walk(amplitudes)

        self.assertEqual(emitted[0], frames[0])

    def test_the_silence_that_closes_the_turn_is_included(self):
        """The recogniser needs the trailing quiet to finalise the transcript."""
        amplitudes = unique_script(utterance())
        gate, frames, emitted, end_index = walk(amplitudes)

        trailing = emitted[-gate.end_silence_frames:]
        self.assertEqual(trailing, frames[end_index + 1 - gate.end_silence_frames:end_index + 1])
        self.assertGreaterEqual(len(trailing), gate.end_silence_frames)

    def test_a_mid_sentence_dip_does_not_lose_a_single_frame(self):
        """Hysteresis keeps the turn open, so a soft word is still retained."""
        amplitudes = unique_script(
            ["quiet"] * 4 + ["loud"] * 8 + ["soft"] * 10 + ["loud"] * 8 + ["silence"] * 12)
        gate, frames, emitted, end_index = walk(amplitudes)

        self.assertIsNotNone(end_index, "a quiet stretch ended the turn")
        self.assertEqual(emitted, frames[:end_index + 1])

    def test_the_preroll_window_can_never_discard_speech(self):
        """The bound is real, and it only ever reaches back over silence.

        A long quiet stretch fills the pre-roll and pushes the oldest silence
        out of it, but the first spoken frame is always inside the window, so
        the start of a turn is never lost.
        """
        kinds = ["quiet"] * 60 + ["loud"] * 12 + ["silence"] * 12
        amplitudes = unique_script(kinds)
        gate, frames, emitted, end_index = walk(amplitudes)
        first_spoken = kinds.index("loud")

        self.assertEqual(
            emitted, frames[:end_index + 1][-len(emitted):],
            "retention was not one contiguous run ending at the close of the turn")
        self.assertLess(
            len(emitted), end_index + 1,
            "with more silence than the pre-roll holds, the oldest must go")
        start = end_index + 1 - len(emitted)
        self.assertLessEqual(
            start, first_spoken,
            "speech was dropped: the pre-roll did not reach back to the first spoken frame")
        for index in range(first_spoken, first_spoken + 12):
            self.assertIn(frames[index], emitted,
                          f"spoken frame {index} went missing")

    def test_frames_are_never_emitted_twice(self):
        amplitudes = unique_script(utterance())
        gate, frames, emitted, end_index = walk(amplitudes)

        self.assertEqual(len(emitted), len(set(emitted)),
                         "a frame was handed to the recogniser more than once")


class BrowserBacklogRetentionTests(unittest.IsolatedAsyncioTestCase):
    """The browser's audio is queued before the gate sees it. That queue must
    not quietly eat the start of a sentence."""

    async def test_audio_below_the_cap_is_delivered_whole_and_in_order(self):
        audio = RemoteAudio()
        pushed = [f"frame-{index}".encode() for index in range(MAX_BACKLOG_FRAMES)]
        for chunk in pushed:
            audio.push(chunk)

        self.assertEqual(audio.dropped_frames, 0)
        self.assertEqual(audio.dropped_bytes, 0)

        received = [await audio.next_audio() for _ in pushed]
        self.assertEqual(received, pushed, "queued audio was reordered or lost")

    async def test_overflow_drops_the_oldest_and_counts_what_it_dropped(self):
        audio = RemoteAudio()
        overflow = 5
        for index in range(MAX_BACKLOG_FRAMES + overflow):
            audio.push(f"frame-{index}".encode())

        self.assertEqual(audio.dropped_frames, overflow)
        self.assertGreater(audio.dropped_bytes, 0, "drops happened but were not counted")

        received = [await audio.next_audio() for _ in range(MAX_BACKLOG_FRAMES)]
        self.assertEqual(received[0], f"frame-{overflow}".encode(),
                         "the newest audio should be kept, not the oldest")

    async def test_the_loss_is_visible_to_the_dashboard(self):
        audio = RemoteAudio()
        payload = frame(900)
        for _ in range(MAX_BACKLOG_FRAMES + 3):
            audio.push(payload)
        microphone = BrowserMicrophone(audio)

        self.assertEqual(microphone.dropped_frames, 3)
        self.assertAlmostEqual(
            microphone.dropped_seconds, 3 * len(payload) / 2 / MICROPHONE_RATE, places=6)

    async def test_a_clean_run_reports_no_loss_at_all(self):
        audio = RemoteAudio()
        payload = frame(900)
        for _ in range(MAX_BACKLOG_FRAMES):
            audio.push(payload)
        microphone = BrowserMicrophone(audio)

        self.assertEqual(microphone.dropped_frames, 0)
        self.assertEqual(microphone.dropped_seconds, 0.0)


class ScriptedMicrophone:
    def __init__(self, frames):
        self._frames = list(frames)

    async def frames(self):
        for pcm in self._frames:
            yield pcm


class ScriptedSpeaker:
    def __init__(self):
        self.plays: list[bytes] = []

    async def play(self, pcm):
        self.plays.append(pcm)

    async def open(self):
        return None

    async def close(self):
        return None


class ScriptedStt:
    def __init__(self, final="hello athena", partials=("hello",)):
        self.turn = None
        self.received: list[bytes] = []
        self.final = final
        self.partials = list(partials)
        self.finished = asyncio.Event()

    async def start_turn(self, turn):
        self.turn = turn

    async def send_audio(self, pcm):
        self.received.append(pcm)

    async def finish_turn(self):
        self.finished.set()

    async def results(self):
        await self.finished.wait()
        for text in self.partials:
            yield Transcript(self.turn, text, False, 0.4)
        yield Transcript(self.turn, self.final, True, 1.0)


def build_coordinator(microphone, speaker, stt):
    settings = MagicMock()
    settings.get.side_effect = {
        "vad_minimum_rms": 500,
        "vad_noise_multiplier": 2.0,
        "vad_end_silence_ms": 220,
        "vad_start_ms": None,
        "vad_minimum_speech_ms": None,
    }.get
    coordinator = VoiceCoordinator(microphone, speaker, stt, MagicMock(), MagicMock(),
                                  MagicMock(), opening_gate(), settings)
    coordinator.active_turn = uuid4()
    return coordinator


def zero_crossing_rate(values) -> float:
    crossings = sum(1 for index in range(1, len(values))
                    if (values[index - 1] < 0) != (values[index] < 0))
    return crossings / max(1, len(values))


class EndOfSpeechToneTests(unittest.IsolatedAsyncioTestCase):
    def test_the_tone_is_audible_and_falls(self):
        values = array("h")
        values.frombytes(end_of_speech_tone(SAMPLE_RATE))

        self.assertGreater(len(values), 0, "the tone is empty")
        self.assertGreater(max(abs(value) for value in values), 1000,
                           "the tone is too quiet to hear")

        high_len = int(SAMPLE_RATE * 0.07)
        gap_len = int(SAMPLE_RATE * 0.015)
        high = zero_crossing_rate(values[:high_len])
        low = zero_crossing_rate(values[high_len + gap_len:])
        self.assertGreater(high, low * 1.4,
                           "the tone should fall, to mirror the rising acknowledgement")

    def test_the_tone_ends_without_a_click(self):
        values = array("h")
        values.frombytes(end_of_speech_tone(SAMPLE_RATE))
        self.assertLess(abs(values[-1]), 64,
                        "the tone stops far from zero and will click")

    async def test_it_plays_exactly_once_when_the_turn_closes(self):
        amplitudes = unique_script(utterance())
        _, frames, _, end_index = walk(amplitudes)
        speaker = ScriptedSpeaker()
        coordinator = build_coordinator(
            ScriptedMicrophone(frames[:end_index + 1]), speaker, ScriptedStt())
        coordinator._eos_pcm = b"the-tone"
        coordinator.eos_tone = True

        await asyncio.wait_for(coordinator._listen(coordinator.active_turn), 5)

        self.assertEqual(speaker.plays, [b"the-tone"],
                         "end of speech did not play the tone exactly once")

    async def test_it_stays_silent_when_turned_off(self):
        amplitudes = unique_script(utterance())
        _, frames, _, end_index = walk(amplitudes)
        speaker = ScriptedSpeaker()
        coordinator = build_coordinator(
            ScriptedMicrophone(frames[:end_index + 1]), speaker, ScriptedStt())
        coordinator._eos_pcm = b"the-tone"
        coordinator.eos_tone = False

        await asyncio.wait_for(coordinator._listen(coordinator.active_turn), 5)

        self.assertEqual(speaker.plays, [], "the tone played while switched off")

    def test_the_tone_is_on_unless_turned_off(self):
        for value, expected in (("1", True), ("0", False), ("off", False),
                                ("", True), ("yes", True)):
            with patch.dict(os.environ, {"ATHENA_EOS_TONE": value}):
                coordinator = VoiceCoordinator(
                    MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                    MagicMock(), MagicMock(), MagicMock(), MagicMock())
                self.assertEqual(coordinator.eos_tone, expected, value)


class RetentionReachesTheRecogniserTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_recogniser_receives_what_the_gate_retained(self):
        amplitudes = unique_script(utterance())
        _, frames, _, end_index = walk(amplitudes)
        stt = ScriptedStt()
        coordinator = build_coordinator(
            ScriptedMicrophone(frames[:end_index + 1]), ScriptedSpeaker(), stt)

        transcript = await asyncio.wait_for(
            coordinator._listen(coordinator.active_turn), 5)

        self.assertEqual(transcript, "hello athena")
        self.assertEqual(
            b"".join(stt.received), b"".join(frames[:end_index + 1]),
            "the recogniser did not receive the whole utterance, in order")

    async def test_nothing_after_the_turn_closes_is_streamed(self):
        """Billing follows the stream, so the frames past the close must stop."""
        amplitudes = unique_script(utterance())
        _, frames, _, end_index = walk(amplitudes)
        stt = ScriptedStt()
        coordinator = build_coordinator(
            ScriptedMicrophone(frames), ScriptedSpeaker(), stt)

        await asyncio.wait_for(coordinator._listen(coordinator.active_turn), 5)

        self.assertEqual(b"".join(stt.received), b"".join(frames[:end_index + 1]))

    async def test_the_recogniser_hears_the_speech_inside_a_long_quiet_run(self):
        kinds = ["quiet"] * 60 + ["loud"] * 12 + ["silence"] * 12
        amplitudes = unique_script(kinds)
        _, frames, _, end_index = walk(amplitudes)
        stt = ScriptedStt()
        coordinator = build_coordinator(
            ScriptedMicrophone(frames[:end_index + 1]), ScriptedSpeaker(), stt)

        await asyncio.wait_for(coordinator._listen(coordinator.active_turn), 5)

        heard = b"".join(stt.received)
        first_spoken = kinds.index("loud")
        for index in range(first_spoken, first_spoken + 12):
            self.assertIn(frames[index], heard,
                          f"spoken frame {index} never reached the recogniser")


class PublishedSpeechTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard reads this file; it is how a person sees what was heard."""

    async def test_the_page_can_see_what_was_heard_and_how_much_was_kept(self):
        amplitudes = unique_script(utterance())
        _, frames, _, end_index = walk(amplitudes)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio-status.json"
            with patch.dict(os.environ, {"ATHENA_AUDIO_STATUS_PATH": str(path)}):
                coordinator = build_coordinator(
                    ScriptedMicrophone(frames[:end_index + 1]), ScriptedSpeaker(),
                    ScriptedStt(final="turn on the kitchen light"))
                await asyncio.wait_for(
                    coordinator._listen(coordinator.active_turn), 5)
            status = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(status["transcript"], "turn on the kitchen light")
        self.assertEqual(status["heard"], "hello")
        self.assertEqual(status["eos_count"], 1)
        self.assertGreater(status["eos_at"], 0)
        self.assertEqual(status["retained_frames"], end_index + 1)
        self.assertGreater(status["retained_seconds"], 0)
        self.assertEqual(status["dropped_frames"], 0)
        self.assertEqual(status["dropped_seconds"], 0.0)

    async def test_the_state_is_waiting_again_once_listening_stops(self):
        amplitudes = unique_script(utterance())
        _, frames, _, end_index = walk(amplitudes)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio-status.json"
            with patch.dict(os.environ, {"ATHENA_AUDIO_STATUS_PATH": str(path)}):
                coordinator = build_coordinator(
                    ScriptedMicrophone(frames[:end_index + 1]), ScriptedSpeaker(),
                    ScriptedStt())
                await asyncio.wait_for(
                    coordinator._listen(coordinator.active_turn), 5)
            status = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(status["state"], "waiting")


class StatusFileTests(unittest.TestCase):
    def test_every_field_survives_the_next_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio-status.json"
            writer = AudioStatusWriter(path)
            writer.state("listening", turn="turn-1")
            writer.update(rms=1.0, noise=2.0, threshold=3.0, speech=True,
                          voiced_frames=4, retained_frames=5, retained_seconds=0.1,
                          dropped_frames=0, dropped_seconds=0.0)
            writer.transcript("hello")
            status = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(status["state"], "listening")
        self.assertEqual(status["transcript"], "hello")
        self.assertEqual(status["retained_frames"], 5)
        self.assertEqual(status["voiced_frames"], 4)
        self.assertEqual(status["turn"], "turn-1")

    def test_retention_fields_are_omitted_when_not_given(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio-status.json"
            AudioStatusWriter(path).update(rms=1.0, noise=1.0, threshold=1.0,
                                           speech=False, voiced_frames=0)
            status = json.loads(path.read_text(encoding="utf-8"))

        self.assertNotIn("retained_frames", status)
        self.assertNotIn("dropped_seconds", status)


class DashboardSpeechEndpointTests(unittest.IsolatedAsyncioTestCase):
    """The page reads the published speech through /api/audio."""

    def _request(self):
        from athena.web import DashboardState
        from athena.web_auth import SessionAuth

        state = DashboardState.__new__(DashboardState)
        state.auth = SessionAuth("", b"a sufficiently long dashboard test secret",
                                 required=False)

        class Request:
            def __init__(self):
                self.app = {"state": state}
                self.cookies = {}

        return Request()

    async def test_it_serves_what_was_heard_and_when_speech_ended(self):
        import time
        from athena.web import speech_status

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio-status.json"
            with patch.dict(os.environ, {"ATHENA_AUDIO_STATUS_PATH": str(path)}):
                writer = AudioStatusWriter(path)
                writer.transcript("turn on the kitchen light")
                writer.heard("turn on the")
                writer.endpoint(turn="t", frames=36, seconds=0.72)
                response = await speech_status(self._request())

        payload = json.loads(response.body)
        self.assertEqual(payload["transcript"], "turn on the kitchen light")
        self.assertEqual(payload["heard"], "turn on the")
        self.assertEqual(payload["retained_frames"], 36)
        self.assertFalse(payload["stale"])
        self.assertIsNotNone(payload["eos_age"])
        self.assertLess(payload["eos_age"], 2)
        self.assertLess(abs(payload["eos_age"] - (time.time() - payload["eos_at"])), 1)

    async def test_it_says_so_when_the_voice_service_is_not_publishing(self):
        from athena.web import speech_status

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio-status.json"
            with patch.dict(os.environ, {"ATHENA_AUDIO_STATUS_PATH": str(path)}):
                response = await speech_status(self._request())

        payload = json.loads(response.body)
        self.assertTrue(payload["stale"])
        self.assertIsNone(payload["age"])
        self.assertIsNone(payload["eos_age"])


if __name__ == "__main__":
    unittest.main()
