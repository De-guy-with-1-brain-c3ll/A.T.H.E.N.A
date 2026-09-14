from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from athena.coordinator import VoiceCoordinator
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry
from athena.tts.qwen import QwenRealtimeSynthesizer
from athena.audio.vad import VoiceGate
from athena.events import Transcript
from athena.config import Settings
from athena.stt.fun_asr import FunAsrRecognizer


class VoiceControlTests(unittest.IsolatedAsyncioTestCase):
    def test_wake_word_filters_background_speech(self):
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        coordinator.wake_word = "athena"
        self.assertIsNone(coordinator._wake_command("the room is quiet"))
        self.assertEqual(coordinator._wake_command("Athena, what time is it"), "what time is it")
        self.assertEqual(coordinator._wake_command("hey a tina weather"), "weather")
        self.assertEqual(coordinator._wake_command("yes", allow_confirmation=True), "yes")
    async def test_local_end_silence_commits_stt_turn(self):
        from array import array
        import asyncio

        def frame(amplitude):
            return array('h', [amplitude] * 320).tobytes()

        class Microphone:
            async def frames(self):
                for pcm in [frame(1000)] * 9 + [frame(0)] * 11:
                    yield pcm

        class Stt:
            def __init__(self):
                self.turn = None
                self.finished = asyncio.Event()
                self.finish_calls = 0
            async def start_turn(self, turn):
                self.turn = turn
            async def send_audio(self, pcm):
                pass
            async def finish_turn(self):
                self.finish_calls += 1
                self.finished.set()
            async def results(self):
                await self.finished.wait()
                yield Transcript(self.turn, 'hello athena', True, 1.0)

        stt = Stt()
        gate = VoiceGate(minimum_rms=500, noise_multiplier=2, end_silence_ms=220)
        settings = MagicMock()
        settings.get.side_effect = {
            'vad_minimum_rms': 500,
            'vad_noise_multiplier': 2.0,
            'vad_end_silence_ms': 220,
        }.get
        coordinator = VoiceCoordinator(Microphone(), MagicMock(), stt, MagicMock(),
            MagicMock(), MagicMock(), gate, settings)
        turn = uuid4()
        coordinator.active_turn = turn
        transcript = await asyncio.wait_for(coordinator._listen(turn), 1)
        self.assertEqual(transcript, 'hello athena')
        self.assertGreaterEqual(stt.finish_calls, 1)

    async def test_short_confirmation_requires_real_pending_state_and_voice_activation(self):
        gate = VoiceGate()
        llm = MagicMock()
        llm.is_confirmation_reply.return_value = True
        coordinator = VoiceCoordinator(MagicMock(), MagicMock(), MagicMock(), llm,
                                       MagicMock(), MagicMock(), gate, MagicMock())
        gate.active = True
        gate.voiced_frames = gate.start_frames  # 100 ms: enough to activate, below 180 ms.
        self.assertFalse(gate.has_enough_speech)
        self.assertTrue(coordinator._accept_transcript('yes'))
        llm.is_confirmation_reply.return_value = False
        self.assertFalse(coordinator._accept_transcript('yes'))
        llm.is_confirmation_reply.return_value = True
        gate.active = False
        self.assertFalse(coordinator._accept_transcript('yes'))

    async def test_english_model_configuration_and_chinese_only_guard(self):
        self.assertEqual(Settings('speech-key', 'llm-key').stt_model, 'fun-asr-realtime')
        with self.assertRaises(ValueError):
            FunAsrRecognizer('test', 'fun-asr-flash-8k-realtime', 16000, 'en')
        with patch('athena.stt.fun_asr.Recognition') as sdk:
            recognizer = FunAsrRecognizer('test', 'fun-asr-realtime', 16000, 'en')
            await recognizer.connect()
            await recognizer.start_turn(uuid4())
            self.assertEqual(sdk.call_args.kwargs['model'], 'fun-asr-realtime')
            self.assertEqual(sdk.call_args.kwargs['language_hints'], ['en'])
            await recognizer.close()

    async def test_shutdown_requires_fresh_direct_user_command(self):
        registry = ToolRegistry.discover()
        self.assertFalse((await registry.execute('shutdown_athena', {})).success)
        self.assertIsNone(await registry.handle_user_command('Do not shut down ATHENA'))
        self.assertFalse((await registry.execute('shutdown_athena', {})).success)
        result = await registry.handle_user_command('ATHENA, shut down.')
        self.assertTrue(result.success)
        self.assertTrue(registry.shutdown_requested)

    async def test_shutdown_bypasses_model_and_coordinator_exits_after_goodbye(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = RuntimeSettingsStore(Path(directory) / 'settings.json')
            registry = ToolRegistry.discover()
            model = DeepSeekLanguageModel('test-key', 'test-model', registry, settings)
            model._client.chat.completions.create = AsyncMock()
            coordinator = VoiceCoordinator(MagicMock(), MagicMock(), MagicMock(), model,
                                           MagicMock(), MagicMock(), MagicMock(), settings)
            coordinator._listen = AsyncMock(return_value='shut down athena')
            spoken = []
            async def answer(turn, text):
                spoken.extend([part async for part in model.stream_reply(turn, text)])
            coordinator._answer = AsyncMock(side_effect=answer)
            try:
                await coordinator.run()
                model._client.chat.completions.create.assert_not_called()
            finally:
                await model.close()
            self.assertEqual(coordinator._listen.await_count, 1)
            self.assertIn('Goodbye', spoken[0])

    async def test_tts_speed_default_and_live_setting_reach_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = RuntimeSettingsStore(Path(directory) / 'settings.json')
            self.assertEqual(settings.get('tts_speech_rate'), 1.2)
            with patch('athena.tts.qwen.QwenTtsRealtime') as sdk:
                tts = QwenRealtimeSynthesizer('test', 'qwen3-tts-flash-realtime', 'Neil', settings)
                await tts.connect()
                await tts.send_text(uuid4(), 'Hello.')
                self.assertEqual(sdk.return_value.update_session.call_args.kwargs['speech_rate'], 1.2)
                settings.set('tts_speech_rate', 1.4)
                await tts.send_text(uuid4(), 'Faster.')
                self.assertEqual(sdk.return_value.update_session.call_args.kwargs['speech_rate'], 1.4)
                await tts.close()
            with self.assertRaises(ValueError): settings.set('tts_speech_rate', 3)
            with self.assertRaises(ValueError): settings.set('tts_speech_rate', 'nan')

    async def test_empty_model_reply_does_not_hang_playback(self):
        async def no_reply(*args):
            if False: yield ''
        async def never_audio():
            import asyncio
            await asyncio.Event().wait()
            if False: yield None
        llm = SimpleNamespace(stream_reply=no_reply)
        tts = SimpleNamespace(audio=never_audio, flush=AsyncMock())
        memory = MagicMock()
        memory.context_messages.return_value = []
        coordinator = VoiceCoordinator(MagicMock(), MagicMock(), MagicMock(), llm, tts, memory, MagicMock(), MagicMock())
        turn = uuid4()
        coordinator.active_turn = turn
        import asyncio
        await asyncio.wait_for(coordinator._answer(turn, 'hello'), 1)
        tts.flush.assert_not_called()
