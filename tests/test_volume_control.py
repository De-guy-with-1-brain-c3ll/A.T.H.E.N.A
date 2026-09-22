"""Tests for the dashboard's speaker volume control.

Volume is applied in `Speaker.play`, which is the one place every sound passes
through, so these cover the two halves that can silently disagree: the scaling
itself, and the level surviving the trip through the dashboard to the voice
process and back.
"""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from athena.audio.playback import Speaker
from athena.coordinator import VoiceCoordinator
from athena.voice_ipc import VoiceControlServer


def pcm(*samples: int) -> bytes:
    from array import array
    import sys
    values = array("h", samples)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def samples_of(pcm_bytes: bytes) -> list[int]:
    from array import array
    values = array("h")
    values.frombytes(pcm_bytes)
    if __import__("sys").byteorder != "little":
        values.byteswap()
    return list(values)


class SpeakerVolumeTests(unittest.TestCase):
    def test_full_volume_returns_the_bytes_untouched(self):
        original = pcm(1000, -2000, 32767, -32768)
        speaker = Speaker()
        self.assertEqual(speaker.volume, 100)
        # Identity matters: scaling at 100% would lose a little to rounding on
        # every replay, and the full-volume path is the common one.
        self.assertIs(speaker._apply_volume(original), original)

    def test_half_volume_halves_every_sample(self):
        speaker = Speaker()
        speaker.set_volume(50)
        self.assertEqual(samples_of(speaker._apply_volume(pcm(1000, -2000, 100))),
                         [500, -1000, 50])

    def test_zero_volume_is_silence_not_an_error(self):
        speaker = Speaker()
        speaker.set_volume(0)
        self.assertEqual(samples_of(speaker._apply_volume(pcm(1000, -2000))), [0, 0])

    def test_out_of_range_levels_are_clamped(self):
        speaker = Speaker()
        self.assertEqual(speaker.set_volume(140), 100)
        self.assertEqual(speaker.set_volume(-20), 0)
        # A level from the API arrives as a string often enough to matter.
        self.assertEqual(speaker.set_volume("45"), 45)

    def test_the_level_is_reported_back_as_a_whole_percentage(self):
        speaker = Speaker()
        for percent in (0, 1, 35, 99, 100):
            self.assertEqual(speaker.set_volume(percent), percent)


class CoordinatorVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_setting_the_level_reaches_the_speaker(self):
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        coordinator.speaker = Speaker()
        coordinator._volume = 100
        self.assertEqual(await coordinator.set_volume(40), 40)
        self.assertEqual(coordinator.speaker.volume, 40)
        # The coordinator is the durable copy: a restart re-applies it.
        self.assertEqual(coordinator.volume, 40)

    async def test_an_absent_speaker_control_does_not_break_the_set(self):
        """A stand-in speaker in the tests has no set_volume; that must not raise."""
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        coordinator.speaker = SimpleNamespace()
        coordinator._volume = 100
        self.assertEqual(await coordinator.set_volume(25), 25)
        self.assertEqual(coordinator.volume, 25)

    async def test_connect_applies_the_remembered_level(self):
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        speaker = Speaker()
        coordinator.speaker = speaker
        coordinator._volume = 30
        self.assertEqual(await coordinator.set_volume(coordinator._volume), 30)
        self.assertEqual(speaker.volume, 30)


class VolumeSocketTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard talks to the voice process over the control socket."""

    def make_writer(self):
        """A writer whose `write` records, so the reply can be inspected."""
        from unittest.mock import MagicMock
        return SimpleNamespace(write=MagicMock(), drain=AsyncMock(),
                               close=lambda: None, wait_closed=AsyncMock())

    def make_server(self, coordinator) -> VoiceControlServer:
        return VoiceControlServer(coordinator, path=None)

    async def test_reading_the_level_needs_no_value(self):
        coordinator = SimpleNamespace(volume=65)
        server = self.make_server(coordinator)
        writer = self.make_writer()
        import json
        reader = SimpleNamespace(readline=AsyncMock(
            return_value=json.dumps({"action": "volume"}).encode() + b"\n"))
        await server._handle(reader, writer)
        sent = json.loads(writer.write.call_args[0][0].decode())
        self.assertEqual(sent, {"ok": True, "volume": 65})

    async def test_setting_the_level_calls_the_coordinator(self):
        coordinator = SimpleNamespace(volume=100, set_volume=AsyncMock(return_value=20))
        server = self.make_server(coordinator)
        writer = self.make_writer()
        import json
        reader = SimpleNamespace(readline=AsyncMock(
            return_value=json.dumps({"action": "volume", "value": 20}).encode() + b"\n"))
        await server._handle(reader, writer)
        coordinator.set_volume.assert_awaited_once_with(20)
        sent = json.loads(writer.write.call_args[0][0].decode())
        self.assertEqual(sent, {"ok": True, "volume": 20})


class DashboardVolumeEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_endpoint_returns_the_level_the_voice_reported(self):
        from athena import web
        request = SimpleNamespace()
        with patch.object(web, "_require_auth"), \
             patch.object(web, "request_volume", AsyncMock(return_value={"ok": True, "volume": 55})):
            response = await web.volume_status(request)
        import json
        self.assertEqual(json.loads(response.body), {"ok": True, "volume": 55})

    async def test_a_missing_value_is_rejected(self):
        from athena import web
        import aiohttp.web as aiohttp_web
        request = SimpleNamespace(json=AsyncMock(return_value={"action": "volume"}))
        with patch.object(web, "_require_post"), \
             patch.object(web, "_json_body", AsyncMock(return_value={})):
            with self.assertRaises(aiohttp_web.HTTPBadRequest):
                await web.volume_control(request)
