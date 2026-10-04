import asyncio
from array import array
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from athena.audio.vad import VoiceGate
from athena.coordinator import VoiceCoordinator
from athena.events import Transcript
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.llm.public_stream import PublicTextStream
from athena.llm.speech_chunker import SpeechChunker
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry


class TextTests(unittest.TestCase):
    def test_private_tags_never_leak_at_any_fragment_boundary(self):
        text = "<think>hidden reasoning</think>Hello.<analysis>secret</analysis> World."
        for size in range(1, len(text)):
            filter = PublicTextStream()
            result = "".join(filter.feed(text[i:i+size]) for i in range(0, len(text), size))
            self.assertEqual(result + filter.finish(), "Hello. World.")

    def test_unclosed_private_analysis_is_discarded(self):
        filter = PublicTextStream()
        self.assertEqual(filter.feed("Hi.<think>secret") + filter.finish(), "Hi.")

    def test_decimals_and_chinese_have_natural_speech_boundaries(self):
        chunker = SpeechChunker()
        self.assertEqual(chunker.feed("It is 22."), [])
        self.assertEqual(chunker.feed("5 degrees. Fine."), ["It is 22.5 degrees.", "Fine."])
        self.assertEqual(SpeechChunker().feed("好的。已经暂停！"), ["好的。", "已经暂停！"])


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_teams_token_refresh_does_not_block_chat_and_is_singleflight(self):
        import threading
        from athena.tools.teams import TeamsGraph
        started = threading.Event()
        release = threading.Event()
        def token(scopes=None):
            started.set()
            release.wait(2)
            return "test-token"
        auth = NS(scopes=["User.Read"], token=MagicMock(side_effect=token))
        graph = TeamsGraph(auth)
        first = asyncio.create_task(graph._access_token())
        second = asyncio.create_task(graph._access_token())
        try:
            for _ in range(100):
                if started.is_set(): break
                await asyncio.sleep(.001)
            self.assertTrue(started.is_set())
            self.assertFalse(first.done())
            release.set()
            self.assertEqual(await asyncio.gather(first, second), ["test-token"]*2)
            self.assertEqual(await graph._access_token(), "test-token")
            auth.token.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            await graph.close()

    async def test_conversation_first_sentence_arrives_before_provider_finishes(self):
        release = asyncio.Event()
        class Stream:
            async def __aiter__(self):
                yield NS(choices=[NS(delta=NS(content="Hello.",tool_calls=[]))])
                await release.wait()
                yield NS(choices=[NS(delta=NS(content=" How are you?",tool_calls=[]))])
            async def close(self): pass
        with tempfile.TemporaryDirectory() as directory:
            model = DeepSeekLanguageModel("test", "test", ToolRegistry(),
                RuntimeSettingsStore(Path(directory)/"settings.json"))
            model._open_stream = AsyncMock(return_value=Stream())
            reply = model.stream_reply(uuid4(), "hello")
            try:
                self.assertEqual(await asyncio.wait_for(anext(reply), 0.5), "Hello.")
                release.set()
                self.assertEqual("".join([p async for p in reply]), " How are you?")
            finally:
                await reply.aclose()
                await model.close()

    def coordinator(self, detector, cloud, frames):
        class Microphone:
            async def frames(self):
                for frame in frames:
                    yield frame
                    await asyncio.sleep(0)
        settings = MagicMock()
        settings.get.side_effect = {"vad_minimum_rms":500,"vad_noise_multiplier":2,
            "vad_end_silence_ms":220}.get
        result = VoiceCoordinator(Microphone(), MagicMock(), cloud, MagicMock(),
            MagicMock(), MagicMock(), VoiceGate(minimum_rms=500), settings,
            local_wake_stt=detector)
        result.wake_word = "athena"
        result.active_turn = uuid4()
        return result

    async def test_keyword_opens_cloud_while_user_is_still_speaking_and_preserves_onset(self):
        speech = array("h", [1000]*320).tobytes()
        silence = bytes(640)
        frames = [speech]*20+[silence]*11
        detector = NS(streaming=True, reset=MagicMock(), process=AsyncMock(side_effect=
            [False]*6+[True]))
        finished = asyncio.Event()
        cloud = NS(start_turn=AsyncMock(), send_audio=AsyncMock(),
                   finish_turn=AsyncMock(side_effect=finished.set))
        coordinator = self.coordinator(detector, cloud, frames)
        async def results():
            await finished.wait()
            yield Transcript(coordinator.active_turn, "Athena hello", True,1)
        cloud.results = results
        self.assertEqual(await asyncio.wait_for(coordinator._listen(coordinator.active_turn),2),
                         "Athena hello")
        self.assertEqual(b"".join(c.args[0] for c in cloud.send_audio.call_args_list), b"".join(frames))
        self.assertEqual(coordinator._keyword_verified_turn, coordinator.active_turn)

    async def test_background_speech_sends_no_cloud_audio(self):
        detector = NS(streaming=True, reset=MagicMock(),process=AsyncMock(return_value=False))
        cloud = MagicMock(start_turn=AsyncMock(),send_audio=AsyncMock(),finish_turn=AsyncMock())
        frames = [array("h",[1000]*320).tobytes()]*20+[bytes(640)]*11
        coordinator = self.coordinator(detector,cloud,frames)
        self.assertEqual(await coordinator._listen(coordinator.active_turn), "")
        cloud.start_turn.assert_not_awaited()
        cloud.send_audio.assert_not_awaited()
        cloud.finish_turn.assert_not_awaited()

    async def test_music_can_be_interrupted_by_local_keyword_without_always_on_cloud(self):
        speech=array("h",[1000]*320).tobytes()
        frames=[speech]*20+[bytes(640)]*11
        detector=NS(streaming=True,reset=MagicMock(),process=AsyncMock(side_effect=[False]*6+[True]))
        cloud=NS(start_turn=AsyncMock(),send_audio=AsyncMock(),finish_turn=AsyncMock())
        coordinator=self.coordinator(detector,cloud,frames)
        coordinator._active_until=time.monotonic()+20
        coordinator.llm._tools.get.return_value=NS(player=NS(status=lambda:{"playing":True,"paused":False}))
        async def results():
            yield Transcript(coordinator.active_turn,'Athena pause music',True,1)
        cloud.results=results
        with patch('athena.coordinator.listen_while_music',return_value=False):
            self.assertEqual(await coordinator._listen(coordinator.active_turn),'Athena pause music')
        detector.process.assert_awaited()
        cloud.start_turn.assert_awaited_once()

    async def test_music_without_keyword_stays_local_even_in_active_window(self):
        detector=NS(streaming=True,reset=MagicMock(),process=AsyncMock(return_value=False))
        cloud=MagicMock(start_turn=AsyncMock(),send_audio=AsyncMock(),finish_turn=AsyncMock())
        frames=[array('h',[1000]*320).tobytes()]*20+[bytes(640)]*11
        coordinator=self.coordinator(detector,cloud,frames)
        coordinator._active_until=time.monotonic()+20
        coordinator.llm._tools.get.return_value=NS(player=NS(status=lambda:{'playing':True,'paused':False}))
        with patch('athena.coordinator.listen_while_music',return_value=False):
            self.assertEqual(await coordinator._listen(coordinator.active_turn),'')
        cloud.start_turn.assert_not_awaited()
        cloud.send_audio.assert_not_awaited()

    async def test_followup_bypasses_local_transcription(self):
        detector = MagicMock(transcribe_once=AsyncMock(side_effect=AssertionError("local decode")))
        cloud = MagicMock(start_turn=AsyncMock(),send_audio=AsyncMock(),finish_turn=AsyncMock())
        frames = [array("h",[1000]*320).tobytes()]*20+[bytes(640)]*11
        coordinator = self.coordinator(detector,cloud,frames)
        coordinator._active_until=time.monotonic()+20
        async def results():
            yield Transcript(coordinator.active_turn,"and tomorrow?",True,1)
        cloud.results=results
        self.assertEqual(await coordinator._listen(coordinator.active_turn),"and tomorrow?")
        detector.transcribe_once.assert_not_awaited()
