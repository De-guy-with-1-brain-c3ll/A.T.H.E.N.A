"""Local SenseVoice recognition: the free backend, driven like the cloud one.

The cloud recognisers bill by the audio second, so the only way to stop paying
is to stop sending the audio anywhere. What these tests protect is that the
local backend is a drop-in for the cloud one: the coordinator drives STT
through one protocol, and a local recogniser that behaves even slightly
differently shows up as a turn that never ends or a reply that answers the
wrong words.
"""
import asyncio
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

from athena.stt import (
    FunAsrRecognizer,
    SenseVoiceRecognizer,
    build_recognizer,
    stt_backend,
)
from athena.stt.fun_asr import PACKET_BYTES
from athena.stt.sensevoice import (
    MINIMUM_DECODE_SECONDS,
    PARTIAL_INTERVAL_SECONDS,
    _TurnBuffer,
    strip_tags,
)


class _FakeStream:
    def __init__(self, result) -> None:
        self.result = result
        self.waveform = None
        self.rate = None

    def accept_waveform(self, rate, waveform) -> None:
        self.rate = rate
        self.waveform = list(waveform)


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeRecognizer:
    """Stands in for sherpa's OfflineRecognizer.

    It records what it was asked to transcribe, so the tests can assert on the
    *shape* of the turn — how much audio a decode saw, how many decodes ran —
    which is what the streaming bridge actually consists of.
    """

    scripts: list[str] = []
    instances: list["FakeRecognizer"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[_FakeStream] = []
        self.script_index = 0
        FakeRecognizer.instances.append(self)

    def create_stream(self) -> _FakeStream:
        return _FakeStream(_FakeResult(""))

    def decode_stream(self, stream: _FakeStream) -> None:
        # Each decode returns the next scripted line, then the last one repeats.
        index = min(self.script_index, len(FakeRecognizer.scripts) - 1)
        self.script_index += 1
        stream.result = _FakeResult(FakeRecognizer.scripts[index] if FakeRecognizer.scripts else "")
        self.calls.append(stream)


def make_recognizer(**kwargs) -> SenseVoiceRecognizer:
    """A recogniser with an injected fake model, bypassing the file checks.

    The loop must be the one the test is actually running on. `_publish` hands
    transcripts over with `call_soon_threadsafe`, so a recogniser holding some
    other loop would schedule the work on a loop nobody is running and the turn
    would end with an empty queue.
    """
    recognizer = SenseVoiceRecognizer(sample_rate=16_000, **kwargs)
    recognizer._recognizer = FakeRecognizer(
        model=str(recognizer._model_file), tokens=str(recognizer._tokens_file))
    recognizer._loop = asyncio.get_running_loop()
    return recognizer


class TagStrippingTests(unittest.TestCase):
    """SenseVoice tags every utterance; the tags must never reach the model."""

    def test_the_leading_tag_run_is_removed(self):
        self.assertEqual(
            strip_tags("<|en|><|NEUTRAL|><|Speech|><|woitn|>Hello there."),
            "Hello there.")

    def test_plain_text_is_left_alone(self):
        self.assertEqual(strip_tags("Hello there."), "Hello there.")

    def test_a_tag_in_the_middle_is_removed_too(self):
        # Half-recognised markers can land anywhere; leaving one would be spoken.
        self.assertEqual(strip_tags("set a timer<|woitn|> please"), "set a timer please")

    def test_an_unclosed_tag_does_not_swallow_the_transcript(self):
        self.assertEqual(strip_tags("Hello <|en"), "Hello")

    def test_leading_whitespace_before_the_tags_is_tolerated(self):
        self.assertEqual(strip_tags("  <|en|><|Speech|>Yes."), "Yes.")


class TurnBufferTests(unittest.TestCase):
    """The growing buffer is what turns a non-streaming model into a stream."""

    def test_seconds_are_counted_from_16_bit_mono_bytes(self):
        buffer = _TurnBuffer(16_000)
        buffer.append(b"\x00\x00" * 16_000)  # exactly one second
        self.assertAlmostEqual(buffer.seconds, 1.0, places=3)

    def test_samples_are_normalised_for_the_model(self):
        buffer = _TurnBuffer(16_000)
        import struct
        buffer.append(struct.pack("<hh", 32767, -32768))
        samples = buffer.samples()
        self.assertAlmostEqual(samples[0], 32767 / 32768.0, places=6)
        self.assertLess(samples[1], -0.99)

    def test_clearing_removes_everything(self):
        buffer = _TurnBuffer(16_000)
        buffer.append(b"\x00\x00" * 100)
        buffer.clear()
        self.assertEqual(buffer.bytes, 0)
        self.assertEqual(buffer.seconds, 0.0)


class DecodeSchedulingTests(unittest.IsolatedAsyncioTestCase):
    """Decoding every packet would run the model ten times a second for nothing."""

    def setUp(self):
        FakeRecognizer.scripts = ["hello world"]
        FakeRecognizer.instances = []

    def _patch(self):
        return patch("athena.stt.sensevoice.SenseVoiceRecognizer._transcribe",
                     lambda self, samples: "hello world")

    async def test_nothing_is_decoded_before_there_is_enough_audio(self):
        recognizer = make_recognizer()
        await recognizer.start_turn(uuid4())
        # One 100 ms packet is below the floor: there is nothing to recognise.
        await recognizer.send_audio(b"\x11\x22" * (PACKET_BYTES // 2))
        self.assertEqual(recognizer.decodes, 0)

    async def test_a_partial_is_produced_once_enough_speech_has_arrived(self):
        recognizer = make_recognizer()
        with self._patch():
            await recognizer.start_turn(uuid4())
            packets = int(PARTIAL_INTERVAL_SECONDS / 0.1) + 1
            for _ in range(packets):
                await recognizer.send_audio(b"\x11\x22" * (PACKET_BYTES // 2))
            self.assertEqual(recognizer.decodes, 1)

    async def test_the_interval_is_measured_in_audio_not_in_calls(self):
        """A slow trickle of audio must not decode more often than a fast one."""
        recognizer = make_recognizer()
        with self._patch():
            await recognizer.start_turn(uuid4())
            # Send the same total audio as one packet, but in pieces.
            per_piece = PACKET_BYTES // 2
            pieces = int((PARTIAL_INTERVAL_SECONDS + MINIMUM_DECODE_SECONDS) * 16_000 * 2
                         / per_piece) + 2
            for _ in range(pieces):
                await recognizer.send_audio(b"\x11\x22" * (per_piece // 2))
            self.assertLessEqual(recognizer.decodes, 2,
                                 "the decode interval is counting calls, not audio")

    async def test_a_partial_is_not_repeated_when_nothing_changed(self):
        recognizer = make_recognizer()
        with self._patch():
            await recognizer.start_turn(uuid4())
            packets = int(PARTIAL_INTERVAL_SECONDS / 0.1) + 1
            for _ in range(packets * 2):
                await recognizer.send_audio(b"\x11\x22" * (PACKET_BYTES // 2))
            await asyncio.sleep(0)
        self.assertGreater(recognizer.decodes, 0)
        # The same words came back twice, so they are announced once.
        self.assertEqual(recognizer.partials, 1)


class TurnLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeRecognizer.scripts = ["hello world"]
        FakeRecognizer.instances = []

    async def _drain(self, recognizer) -> list:
        """Everything published so far.

        Transcripts are handed over with `call_soon_threadsafe`, so the queue is
        filled on the next tick rather than by the time the turn method returns.
        Reading immediately would see an empty queue and read as "the sentinel
        was never published", which is the opposite of what happens.
        """
        await asyncio.sleep(0)
        collected = []
        while not recognizer._results.empty():
            collected.append(recognizer._results.get_nowait())
        return collected

    async def test_a_spoken_turn_ends_with_a_transcript_and_a_complete_sentinel(self):
        recognizer = make_recognizer()
        turn = uuid4()
        with patch.object(SenseVoiceRecognizer, "_transcribe",
                          lambda self, samples: "turn the light on"):
            await recognizer.start_turn(turn)
            packets = int(PARTIAL_INTERVAL_SECONDS / 0.1) + 1
            for _ in range(packets):
                await recognizer.send_audio(b"\x11\x22" * (PACKET_BYTES // 2))
            await recognizer.finish_turn()
            self.assertEqual(recognizer._turn_id, None)
            results = await self._drain(recognizer)
        final = [item for item in results if item.is_final]
        self.assertTrue(any(item.text == "turn the light on" for item in final),
                       "the final decode did not reach the coordinator")
        self.assertEqual(results[-1].text, "[STT complete]")
        self.assertEqual(results[-1].turn_id, turn)

    async def test_a_silent_turn_still_publishes_the_sentinel(self):
        # Without it the coordinator sits waiting for a turn that will never end.
        recognizer = make_recognizer()
        turn = uuid4()
        await recognizer.start_turn(turn)
        await recognizer.send_audio(b"\x00\x00" * 200)  # 12 ms of nothing
        await recognizer.finish_turn()
        results = await self._drain(recognizer)
        self.assertEqual([item.text for item in results], ["[STT complete]"])

    async def test_the_final_decode_sees_the_whole_utterance(self):
        """Partials are for the user; the model gets everything."""
        recognizer = make_recognizer()
        captured: list[int] = []
        with patch.object(SenseVoiceRecognizer, "_transcribe",
                          lambda self, samples: captured.append(len(samples)) or "ok"):
            await recognizer.start_turn(uuid4())
            for _ in range(20):
                await recognizer.send_audio(b"\x11\x22" * (PACKET_BYTES // 2))
            await recognizer.finish_turn()
        self.assertGreater(len(captured), 1, "no decode ran at all")
        self.assertEqual(captured[-1], max(captured),
                         "the last decode saw less audio than an earlier one")

    async def test_a_new_turn_starts_with_no_audio_from_the_last_one(self):
        recognizer = make_recognizer()
        seen: list[int] = []
        with patch.object(SenseVoiceRecognizer, "_transcribe",
                          lambda self, samples: seen.append(len(samples)) or "ok"):
            await recognizer.start_turn(uuid4())
            for _ in range(12):
                await recognizer.send_audio(b"\x11\x22" * (PACKET_BYTES // 2))
            await recognizer.finish_turn()
            first_turn_samples = max(seen)
            seen.clear()
            await recognizer.start_turn(uuid4())
            for _ in range(12):
                await recognizer.send_audio(b"\x11\x22" * (PACKET_BYTES // 2))
            await recognizer.finish_turn()
        self.assertEqual(max(seen), first_turn_samples,
                         "the second turn was decoded with the first turn's audio")

    async def test_audio_is_ignored_before_a_turn_starts(self):
        recognizer = make_recognizer()
        await recognizer.send_audio(b"\x11\x22" * (PACKET_BYTES // 2))
        self.assertEqual(recognizer._buffer.bytes, 0)

    async def test_a_decode_failure_is_reported_rather_than_hanging_the_turn(self):
        recognizer = make_recognizer()
        turn = uuid4()
        with patch.object(SenseVoiceRecognizer, "_transcribe",
                          side_effect=RuntimeError("model exploded")):
            await recognizer.start_turn(turn)
            for _ in range(int(PARTIAL_INTERVAL_SECONDS / 0.1) + 2):
                await recognizer.send_audio(b"\x11\x22" * (PACKET_BYTES // 2))
            results = await self._drain(recognizer)
        self.assertTrue(any(item.text.startswith("[STT error]") for item in results))


class BackendSelectionTests(unittest.TestCase):
    def test_the_default_backend_is_the_cloud_recogniser(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_STT_BACKEND", None)
            self.assertEqual(stt_backend(), "qwen")

    def test_asking_for_sensevoice_without_it_installed_falls_back_and_says_so(self):
        class _Settings:
            dashscope_api_key = "test-key"
            stt_model = "fun-asr-realtime"
            stt_sample_rate = 16_000
            stt_language = "en"
            stt_prewarm = False
            stt_max_sentence_silence_ms = None
            stt_semantic_punctuation = False

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                "ATHENA_STT_BACKEND": "sensevoice",
                "ATHENA_SENSEVOICE_MODEL_DIR": directory,
            }):
                recognizer = build_recognizer(_Settings())
        # A silent substitution would leave ATHENA unable to hear at all.
        self.assertIsInstance(recognizer, FunAsrRecognizer)

    def test_the_model_directory_can_be_pointed_somewhere_else(self):
        from athena.stt.sensevoice import model_directory
        with patch.dict(os.environ, {"ATHENA_SENSEVOICE_MODEL_DIR": "/opt/athena/models"}):
            self.assertEqual(model_directory(), Path("/opt/athena/models"))

    def test_int8_is_the_default_and_fp32_can_be_asked_for(self):
        from athena.stt.sensevoice import sensevoice_model_file
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_SENSEVOICE_PRECISION", None)
            self.assertTrue(sensevoice_model_file().name.endswith("int8.onnx"))
        with patch.dict(os.environ, {"ATHENA_SENSEVOICE_PRECISION": "fp32"}):
            self.assertEqual(sensevoice_model_file().name, "model.onnx")

    def test_the_thread_count_is_bounded_and_defaults_sensibly(self):
        from athena.stt.sensevoice import sensevoice_threads
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_SENSEVOICE_THREADS", None)
            os.environ.pop("sherpa_threads", None)
            self.assertEqual(sensevoice_threads(), 2)
        with patch.dict(os.environ, {"ATHENA_SENSEVOICE_THREADS": "0"}):
            self.assertEqual(sensevoice_threads(), 1, "zero threads cannot run")
        with patch.dict(os.environ, {"ATHENA_SENSEVOICE_THREADS": "nonsense"}):
            self.assertEqual(sensevoice_threads(), 2)


class ConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_missing_model_is_refused_with_a_clear_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            recognizer = SenseVoiceRecognizer(
                model_file=Path(directory) / "absent.onnx",
                tokens_file=Path(directory) / "tokens.txt")
            # Refused at connect, not halfway through the first utterance.
            with self.assertRaises(RuntimeError) as caught:
                await recognizer.connect()
        self.assertIn("absent.onnx", str(caught.exception))

    async def test_a_missing_tokens_file_is_reported_separately(self):
        # A missing tokens file and a missing model have different fixes, so
        # they must not share one message.
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "model.int8.onnx").write_bytes(b"x" * 1024)
            recognizer = SenseVoiceRecognizer(
                model_file=folder / "model.int8.onnx",
                tokens_file=folder / "tokens.txt")
            with self.assertRaises(RuntimeError) as caught:
                await recognizer.connect()
        self.assertIn("tokens", str(caught.exception))

    async def test_a_real_model_loads_and_transcribes_a_real_clip(self):
        """Against the actual model and real speech, when both are there.

        Skipped rather than faked on a machine that has not downloaded them: the
        point is that our sherpa-onnx arguments, the tag stripping and the
        streaming bridge all agree with the real thing, which no fake can show.
        """
        from athena.stt.sensevoice import sensevoice_available
        available, reason = sensevoice_available()
        if not available:
            self.skipTest(f"the SenseVoice model is not downloaded here ({reason})")
        clip = Path("outputs/audio-tests/stt-clips/cj.wav")
        if not clip.is_file():
            self.skipTest("no speech clips (run tools/bench_stt.py first)")

        with wave.open(str(clip), "rb") as handle:
            rate = handle.getframerate()
            pcm = handle.readframes(handle.getnframes())

        recognizer = SenseVoiceRecognizer(sample_rate=rate, threads=2)
        await recognizer.connect()
        self.assertIsNotNone(recognizer._recognizer)
        self.assertGreater(recognizer.last_load_ms, 0.0)
        try:
            await recognizer.start_turn(uuid4())
            # Fed in 100 ms packets, which is how the coordinator delivers audio.
            step = int(rate * 0.1) * 2
            for start in range(0, len(pcm), step):
                await recognizer.send_audio(pcm[start:start + step])
            await recognizer.finish_turn()
            await asyncio.sleep(0)
            results = []
            while not recognizer._results.empty():
                results.append(recognizer._results.get_nowait())
        finally:
            await recognizer.close()

        texts = [item.text for item in results]
        self.assertEqual(texts[-1], "[STT complete]")
        finals = [item.text for item in results
                  if item.is_final and item.text != "[STT complete]"]
        self.assertTrue(finals, "a real utterance produced no transcript")
        # The sentence is "The meeting with CJ is at four thirty this afternoon."
        joined = " ".join(finals).casefold()
        self.assertIn("cj", joined, f"the clip's own words were not recognised: {finals}")
        self.assertIn("meeting", joined)
        # Tags are metadata, not speech: they must never survive into a transcript.
        for text in finals:
            self.assertNotIn("<|", text)


if __name__ == "__main__":
    unittest.main()
