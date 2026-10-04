import asyncio
from array import array
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock
from athena.audio.routing import AudioRouter
from athena.coordinator import VoiceCoordinator

def pair():
    microphone = NS(open=AsyncMock(), close=AsyncMock())
    speaker = NS(open=AsyncMock(), close=AsyncMock(), stop=AsyncMock(), play=AsyncMock(),
                 _sample_rate=24000, music_format=(24000, 1))
    return microphone, speaker

class AudioRoutingTests(unittest.IsolatedAsyncioTestCase):
    def router(self, connected=True):
        self.pi = pair(); self.computer = pair()
        self.bridge = NS(attached=connected, drain=Mock(), send_json=AsyncMock())
        self.router_instance = AudioRouter(self.bridge, lambda: self.pi, lambda: self.computer)
        return self.router_instance

    async def test_handoff_switches_both_and_reuses_the_music_speaker(self):
        router = self.router(); stable = router.speaker
        await asyncio.gather(router.microphone.open(), router.speaker.open())
        self.pi[0].open.assert_awaited_once(); self.pi[1].open.assert_awaited_once()
        await router.switch("computer")
        self.assertIs(router.speaker, stable)
        self.assertEqual(router.status()["target"], "computer")
        self.pi[0].close.assert_awaited_once(); self.pi[1].close.assert_awaited_once()
        await stable.play(b"pcm")
        self.computer[1].play.assert_awaited_once()
        await router.switch("pi")
        self.assertEqual(router.target, "pi")

    async def test_computer_must_be_connected_and_bad_pi_does_not_lose_working_audio(self):
        router = self.router(False)
        with self.assertRaisesRegex(RuntimeError, "unchanged"): await router.switch("computer")
        self.assertEqual(router.target, "pi")
        self.bridge.attached = True
        await router.switch("computer")
        self.pi[0].open.side_effect = OSError("no microphone")
        with self.assertRaisesRegex(RuntimeError, "unchanged"): await router.switch("pi")
        self.assertEqual(router.target, "computer")
        self.assertIs(router.input, self.computer[0])

    async def test_stereo_music_already_in_flight_is_converted_for_pi(self):
        router = self.router()
        await router.speaker.play(array("h", [1000, 3000] * 4800).tobytes(), 48000, 2)
        pcm = self.pi[1].play.await_args.args[0]
        self.assertEqual(len(pcm), 4800)
        samples = array("h"); samples.frombytes(pcm)
        self.assertEqual(samples[0], 2000)

    async def test_voice_command_switches_without_model_call(self):
        c = VoiceCoordinator.__new__(VoiceCoordinator)
        c._switch_audio = AsyncMock(return_value={"target": "computer"})
        c._speak_text = AsyncMock()
        self.assertTrue(await c._handle_fast_audio_control("go ahead and switch ur audio output to my computer"))
        c._switch_audio.assert_awaited_once_with("computer")
        self.assertTrue(await c._handle_fast_audio_control("switch back to the pi"))
        c._switch_audio.assert_awaited_with("pi")
        self.assertFalse(await c._handle_fast_audio_control("open a browser page on my computer"))

    async def test_invalid_targets_rejected_and_status_is_read_only(self):
        router = self.router()
        with self.assertRaises(ValueError): await router.switch("someone-else")
        self.assertEqual(router.status()["target"], "pi")
        self.pi[0].open.assert_not_awaited()

