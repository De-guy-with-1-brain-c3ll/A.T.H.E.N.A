"""The local harness must exercise the real local paths, not a re-implementation."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from athena.alerts import AlertScheduler
from athena.audio.vad import VoiceGate
from athena.dev.fakes import (
    DiscardingSpeaker,
    OfflineLanguageModel,
    ScriptedMicrophone,
    SilentSynthesizer,
    TranscriptRecognizer,
    silence_frames,
    speech_frames,
)
from athena.settings.store import RuntimeSettingsStore


class OfflineModelTests(unittest.IsolatedAsyncioTestCase):
    """The harness must run the production fast path, or it proves nothing."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    async def _model(self):
        from athena.tools.registry import ToolRegistry
        alerts = AlertScheduler(lambda _text: True, path=self.root / "alerts.json")
        registry = ToolRegistry.discover(services={"settings": None,
                                                   "alert_scheduler": alerts})
        return OfflineLanguageModel(registry), alerts, registry

    async def test_an_alarm_phrase_reaches_the_real_tool(self):
        model, alerts, _ = await self._model()
        answer = "".join([part async for part in model.stream_reply(
            uuid4(), "athena set an alarm for 3 seconds")])
        # A short request is confirmed as a timer, with a countdown and a clock time.
        self.assertIn("set:", answer.casefold())
        self.assertIn("seconds from now", answer)
        self.assertEqual(len(alerts.rows()), 1, "the alarm was not really stored")

    async def test_an_unparsed_time_reports_the_real_error(self):
        model, alerts, _ = await self._model()
        answer = "".join([part async for part in model.stream_reply(
            uuid4(), "set an alarm for whenever")])
        self.assertIn("Use a time like", answer)
        self.assertEqual(alerts.rows(), [])

    async def test_an_ordinary_question_does_not_invent_an_alarm(self):
        model, alerts, _ = await self._model()
        answer = "".join([part async for part in model.stream_reply(uuid4(), "hello there")])
        self.assertEqual(answer, "I heard: hello there")
        self.assertEqual(alerts.rows(), [])

    async def test_the_shutdown_flag_comes_from_the_real_registry(self):
        model, alerts, registry = await self._model()
        self.assertFalse(model.shutdown_requested)
        registry.shutdown_requested = True
        self.assertTrue(model.shutdown_requested)


class FakeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_scripted_microphone_keeps_a_quiet_room_after_its_script(self):
        """Ending the stream would make the coordinator spin on empty listens."""
        microphone = ScriptedMicrophone(speech_frames(frames=2))
        frames = microphone.frames()
        try:
            first = await asyncio.wait_for(frames.__anext__(), 1)
            await asyncio.wait_for(frames.__anext__(), 1)
            third = await asyncio.wait_for(frames.__anext__(), 2)
        finally:
            await frames.aclose()
        self.assertEqual(len(first), 640)
        self.assertEqual(len(third), 640)

    async def test_the_recognizer_waits_for_enough_audio(self):
        recognizer = TranscriptRecognizer(["hello"], min_audio_bytes=2000)
        await recognizer.start_turn(uuid4())
        await recognizer.send_audio(b"\x00" * 500)
        self.assertTrue(recognizer._results.empty())
        await recognizer.send_audio(b"\x00" * 2000)
        self.assertFalse(recognizer._results.empty())

    async def test_the_recognizer_runs_out_of_scripts_gracefully(self):
        recognizer = TranscriptRecognizer([], min_audio_bytes=10)
        await recognizer.start_turn(uuid4())
        await recognizer.send_audio(b"\x00" * 100)
        result = recognizer._results.get_nowait()
        self.assertEqual(result.text, "[STT complete]")

    async def test_the_synthesizer_records_speech_and_ends_the_turn(self):
        synthesizer = SilentSynthesizer()
        turn = uuid4()
        await synthesizer.send_text(turn, "hello")
        self.assertEqual(synthesizer.spoken, ["hello"])
        chunks = []
        stream = synthesizer.audio(turn)
        try:
            chunks.append(await asyncio.wait_for(stream.__anext__(), 1))
        finally:
            await stream.aclose()
        self.assertEqual(len(chunks[0].pcm), 480)
        await synthesizer.flush(turn)
        self.assertEqual([chunk async for chunk in synthesizer.audio(turn)], [])

    async def test_the_speaker_accepts_and_discards_audio(self):
        speaker = DiscardingSpeaker()
        await speaker.open()
        await speaker.play(b"\x01\x02" * 10)
        await speaker.stop()
        await speaker.close()
        self.assertEqual(len(speaker.played), 20)
        self.assertEqual(speaker._sample_rate, 24000)


class HarnessScenarioTests(unittest.IsolatedAsyncioTestCase):
    """The scenario scripts must run against a throwaway data directory."""

    async def test_the_sandbox_environment_never_points_at_the_real_data(self):
        from athena.dev.harness import sandbox_environment
        with tempfile.TemporaryDirectory() as directory:
            previous = dict(os.environ)
            try:
                sandbox_environment(Path(directory))
                self.assertEqual(os.environ["ATHENA_DATA_DIR"], directory)
                self.assertTrue(os.environ["ATHENA_DATABASE_PATH"].startswith(directory))
                self.assertNotIn("MICROSOFT_CLIENT_ID", os.environ)
            finally:
                os.environ.clear()
                os.environ.update(previous)

    async def test_the_vad_scenario_runs_and_reports_an_opening(self):
        import contextlib
        import io

        from athena.dev.harness import scenario_vad
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.assertEqual(scenario_vad(), 0)
        # The scenario must actually show the gate opening, not just exit zero.
        self.assertIn("opened at frame 12", captured.getvalue())
        self.assertIn("enough speech to accept a transcript: True", captured.getvalue())

    async def test_settings_used_by_the_harness_disable_memory_calls(self):
        from athena.dev.harness import build_settings
        with tempfile.TemporaryDirectory() as directory:
            store = build_settings(Path(directory))
            self.assertFalse(store.get("memory_enabled"))


if __name__ == "__main__":
    unittest.main()
