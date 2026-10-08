"""Speech recognition latency: the websocket handshake must leave the hot path."""
import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from athena.stt.fun_asr import PREWARM_TTL_SECONDS, FunAsrRecognizer


class FakeRecognition:
    """Stands in for the DashScope session so the handshake can be counted."""

    instances: list["FakeRecognition"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.frames: list[bytes] = []
        self.fail_next_send = False
        FakeRecognition.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def send_audio_frame(self, pcm: bytes) -> None:
        if self.fail_next_send:
            self.fail_next_send = False
            raise RuntimeError("request has been stopped")
        self.frames.append(pcm)


class RecognizerLatencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeRecognition.instances = []
        patcher = patch("athena.stt.fun_asr.Recognition", FakeRecognition)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _recognizer(self, **kwargs):
        return FunAsrRecognizer("key", "fun-asr-realtime", 16_000, "en", **kwargs)

    async def test_connect_opens_a_session_ready_for_the_first_word(self):
        recognizer = self._recognizer()
        await recognizer.connect()
        self.assertEqual(len(FakeRecognition.instances), 1)
        self.assertTrue(FakeRecognition.instances[0].started)

    async def test_start_turn_reuses_the_warm_session(self):
        """A second handshake is exactly the latency this removes."""
        recognizer = self._recognizer()
        await recognizer.connect()
        await recognizer.start_turn(uuid4())
        self.assertEqual(len(FakeRecognition.instances), 1,
                         "the websocket handshake was paid again")
        self.assertEqual(recognizer.reused_sessions, 1)

    async def test_an_expired_warm_session_is_replaced(self):
        recognizer = self._recognizer()
        await recognizer.connect()
        # Pretend the session has been waiting longer than the service tolerates.
        recognizer._prewarm_started_at -= PREWARM_TTL_SECONDS + 5
        await recognizer.start_turn(uuid4())
        self.assertEqual(len(FakeRecognition.instances), 2)
        self.assertTrue(FakeRecognition.instances[0].stopped)
        self.assertEqual(recognizer.reused_sessions, 0)

    async def test_a_dead_warm_session_reconnects_without_losing_the_utterance(self):
        recognizer = self._recognizer()
        await recognizer.connect()
        await recognizer.start_turn(uuid4())
        FakeRecognition.instances[0].fail_next_send = True
        await recognizer.send_audio(b"\x11\x22" * 10)
        self.assertEqual(recognizer.reconnects, 1)
        self.assertEqual(FakeRecognition.instances[-1].frames, [b"\x11\x22" * 10],
                         "the first packet of the utterance was dropped")

    async def test_a_send_failure_mid_turn_is_reported_not_hidden(self):
        recognizer = self._recognizer(prewarm=False)
        await recognizer.connect()
        turn = uuid4()
        await recognizer.start_turn(turn)
        FakeRecognition.instances[0].fail_next_send = True
        # The reconnect attempt fails too, so the turn must report the failure
        # rather than waiting forever for a transcript that cannot arrive.
        with patch.object(recognizer, "_open", side_effect=RuntimeError("network down")):
            await recognizer.send_audio(b"\x11\x22" * 10)
        for _ in range(50):
            await asyncio.sleep(0.01)  # the callback is delivered on the next tick
            if not recognizer._results.empty():
                break
        result = recognizer._results.get_nowait()
        self.assertEqual(result.turn_id, turn)
        self.assertIn("connection closed", result.text)

    async def test_prewarm_can_be_turned_off_to_compare(self):
        recognizer = self._recognizer(prewarm=False)
        await recognizer.connect()
        self.assertEqual(FakeRecognition.instances, [], "pre-warm was not disabled")
        await recognizer.start_turn(uuid4())
        self.assertEqual(len(FakeRecognition.instances), 1)
        self.assertEqual(recognizer.reused_sessions, 0)

    async def test_finish_turn_gets_the_next_session_ready(self):
        recognizer = self._recognizer()
        await recognizer.connect()
        await recognizer.start_turn(uuid4())
        await recognizer.finish_turn()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(FakeRecognition.instances) >= 2:
                break
        self.assertEqual(len(FakeRecognition.instances), 2,
                         "no session was prepared for the next utterance")

    async def test_transcripts_are_reported_against_the_current_turn(self):
        """A warm session is opened before any turn exists, so the id cannot be
        captured when its callback is built."""
        recognizer = self._recognizer()
        await recognizer.connect()
        turn = uuid4()
        await recognizer.start_turn(turn)
        callback = FakeRecognition.instances[0].kwargs["callback"]
        callback.on_error(NS(message="boom"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not recognizer._results.empty():
                break
        result = recognizer._results.get_nowait()
        self.assertEqual(result.turn_id, turn)
        self.assertIn("boom", result.text)

    async def test_late_event_from_previous_session_keeps_its_original_turn(self):
        """A late final/error must never be relabelled as the next sentence."""
        recognizer = self._recognizer(prewarm=False)
        await recognizer.connect()
        first, second = uuid4(), uuid4()
        await recognizer.start_turn(first)
        old_callback = FakeRecognition.instances[-1].kwargs["callback"]
        await recognizer.finish_turn()
        await recognizer.start_turn(second)
        old_callback.on_error(NS(message="late old session"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not recognizer._results.empty():
                break
        result = recognizer._results.get_nowait()
        self.assertEqual(result.turn_id, first)
        self.assertNotEqual(result.turn_id, second)

    async def test_starting_after_finish_cannot_be_overwritten_by_prewarm(self):
        recognizer = self._recognizer()
        await recognizer.connect()
        await recognizer.start_turn(uuid4())
        await recognizer.finish_turn()
        turn = uuid4()
        await recognizer.start_turn(turn)
        await recognizer.send_audio(b"\x11\x22" * 10)
        active = FakeRecognition.instances[-1]
        self.assertEqual(len(FakeRecognition.instances), 2,
                         'The in-flight prewarm should be reused, not followed by another handshake')
        self.assertEqual(recognizer.reused_sessions, 2)
        self.assertEqual(active.frames, [b"\x11\x22" * 10])
        self.assertEqual(active.kwargs["callback"].turn_id, turn)

    async def test_close_stops_prewarming_and_tears_down(self):
        recognizer = self._recognizer()
        await recognizer.connect()
        await recognizer.start_turn(uuid4())
        await recognizer.close()
        self.assertTrue(FakeRecognition.instances[0].stopped)
        self.assertFalse(recognizer._prewarm_enabled)

    async def test_the_sentence_silence_override_is_passed_through(self):
        recognizer = self._recognizer(max_sentence_silence_ms=300)
        await recognizer.connect()
        self.assertEqual(FakeRecognition.instances[0].kwargs["max_sentence_silence"], 300)

    async def test_no_sentence_silence_override_by_default(self):
        recognizer = self._recognizer()
        await recognizer.connect()
        self.assertNotIn("max_sentence_silence", FakeRecognition.instances[0].kwargs)

    def test_the_chinese_only_8k_model_is_still_guarded(self):
        with self.assertRaises(ValueError):
            FunAsrRecognizer("key", "fun-asr-flash-8k-realtime", 8000, "en")


if __name__ == "__main__":
    unittest.main()


class SpeechRecognitionHintsTests(unittest.TestCase):
    """Mixed English and Chinese made the recogniser guess at his words."""

    def _kwargs(self, languages=None, vocabulary=None):
        import os
        from unittest.mock import patch
        from athena.stt.fun_asr import FunAsrRecognizer
        env = {}
        if languages is not None:
            env["ATHENA_STT_LANGUAGES"] = languages
        if vocabulary is not None:
            env["ATHENA_STT_VOCABULARY_ID"] = vocabulary
        with patch.dict(os.environ, env, clear=False):
            for name in ("ATHENA_STT_LANGUAGES", "ATHENA_STT_VOCABULARY_ID"):
                if name not in env:
                    os.environ.pop(name, None)
            recogniser = FunAsrRecognizer.__new__(FunAsrRecognizer)
            recogniser._model = "fun-asr-realtime"
            recogniser._sample_rate = 16_000
            recogniser._language = "en"
            recogniser._languages = [
                part.strip() for part in os.environ.get("ATHENA_STT_LANGUAGES", "").split(",")
                if part.strip()] or ["en"]
            recogniser._vocabulary_id = os.environ.get("ATHENA_STT_VOCABULARY_ID", "").strip()
            recogniser._max_sentence_silence_ms = None
        return recogniser._session_kwargs()

    def test_the_language_defaults_to_english_alone(self):
        self.assertEqual(self._kwargs()["language_hints"], ["en"])

    def test_more_than_one_language_can_be_hinted(self):
        self.assertEqual(self._kwargs(languages="en,zh")["language_hints"], ["en", "zh"])

    def test_a_vocabulary_is_only_sent_when_one_is_configured(self):
        self.assertNotIn("vocabulary_id", self._kwargs())
        self.assertEqual(self._kwargs(vocabulary="vocab-1")["vocabulary_id"], "vocab-1")


class EndOfSpeechTests(unittest.TestCase):
    """220-260 ms of silence ended the turn mid-sentence. That was the cut-off."""

    def test_the_default_outlasts_a_natural_pause(self):
        from athena.config import Settings
        from athena.settings.store import CATALOG
        spec = CATALOG["vad_end_silence_ms"]
        # People pause 300-700 ms between clauses while thinking.
        self.assertGreaterEqual(spec.default, 500,
                                "the end-of-speech threshold cuts natural pauses")
        # 750 ms is the value Benjamin settled on: no mid-sentence cut-offs,
        # without a full second of dead air after every command.
        self.assertEqual(Settings.__dataclass_fields__["vad_end_silence_ms"].default, 750)
        self.assertEqual(spec.default, 750)

    def test_the_service_default_is_not_overridden_without_measurement(self):
        """A live server threshold can add seconds after capture has ended."""
        from athena.config import Settings
        self.assertIsNone(Settings.__dataclass_fields__["stt_max_sentence_silence_ms"].default)

    def test_it_can_still_be_tuned_both_ways(self):
        from athena.settings.store import CATALOG
        spec = CATALOG["vad_end_silence_ms"]
        self.assertLessEqual(spec.minimum, 300, "should be able to make it snappier")
        self.assertGreaterEqual(spec.maximum, 1200, "should be able to make it patient")

    def test_the_description_says_what_to_do_about_it(self):
        from athena.settings.store import CATALOG
        description = CATALOG["vad_end_silence_ms"].description.casefold()
        self.assertIn("cuts you off", description)


class MixedLanguageTests(unittest.TestCase):
    """He speaks English with Chinese words in it; one hint was not enough."""

    def _hints(self, value):
        import os
        from unittest.mock import patch
        from athena.stt.fun_asr import FunAsrRecognizer
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_STT_LANGUAGES", None)
            recogniser = FunAsrRecognizer.__new__(FunAsrRecognizer)
            recogniser._model = "qwen-audio-3.0-asr-flash-streaming"
            recogniser._sample_rate = 16_000
            recogniser._language = value
            configured = os.environ.get("ATHENA_STT_LANGUAGES", "").strip() or value
            recogniser._languages = [
                part.strip() for part in configured.split(",") if part.strip()] or ["en"]
            recogniser._vocabulary_id = ""
            recogniser._max_sentence_silence_ms = None
        return recogniser._session_kwargs()["language_hints"]

    def test_one_language_stays_one_hint(self):
        self.assertEqual(self._hints("en"), ["en"])

    def test_a_list_becomes_several_hints(self):
        self.assertEqual(self._hints("en,zh"), ["en", "zh"])

    def test_the_dashboard_offers_the_mixed_option(self):
        from athena.settings.store import CATALOG
        self.assertIn("en,zh", CATALOG["stt_language"].choices)
