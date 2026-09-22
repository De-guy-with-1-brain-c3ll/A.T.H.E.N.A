"""Edge speech: the free cloud voice, and the protocol that carries it.

The network is not touched here. What is tested is the part that was written: the
two-hop transport's plumbing, the turn sentinel on every path, and the settings —
including the one setting whose *absence* is the point.
"""
import asyncio
import os
import shutil
from unittest.mock import patch
from uuid import uuid4

import unittest

from athena.events import AudioChunk
from athena.tts import (
    EdgeSynthesizer,
    QwenRealtimeSynthesizer,
    build_synthesizer,
    speech_cost_label,
    speech_is_billed,
    synthesizer_sample_rate,
)
from athena.tts.edge import (
    DEFAULT_VOICE,
    SAMPLE_RATE,
    decoder_command,
    edge_available,
    edge_pitch,
    edge_rate,
    edge_sample_rate,
    edge_voice,
)


class _Settings:
    dashscope_api_key = "test-key"
    tts_model = "qwen3-tts-flash-realtime"
    tts_voice = "Dolce"
    tts_sample_rate = 24_000


class SettingsTests(unittest.TestCase):
    def test_the_voice_defaults_to_aria(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_EDGE_VOICE", None)
            self.assertEqual(edge_voice(), DEFAULT_VOICE)
            self.assertEqual(DEFAULT_VOICE, "en-US-AriaNeural")

    def test_a_configured_voice_wins(self):
        with patch.dict(os.environ, {"ATHENA_EDGE_VOICE": "en-GB-RyanNeural"}):
            self.assertEqual(edge_voice(), "en-GB-RyanNeural")

    def test_rate_and_pitch_are_unset_by_default(self):
        # Not cosmetic: sending a no-op pitch measured 1.2-2.0s to first audio
        # against 0.85s when nothing was sent. An unset value must stay unset, or
        # the default silently costs 2.3 seconds on every reply.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_EDGE_RATE", None)
            os.environ.pop("ATHENA_EDGE_PITCH", None)
            self.assertEqual(edge_rate(), "")
            self.assertEqual(edge_pitch(), "")

    def test_rate_and_pitch_are_passed_through_when_set(self):
        with patch.dict(os.environ, {"ATHENA_EDGE_RATE": "+10%",
                                     "ATHENA_EDGE_PITCH": "+5Hz"}):
            self.assertEqual(edge_rate(), "+10%")
            self.assertEqual(edge_pitch(), "+5Hz")

    def test_the_sample_rate_defaults_to_24k(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_EDGE_SAMPLE_RATE", None)
            self.assertEqual(edge_sample_rate(), 24_000)
            self.assertEqual(SAMPLE_RATE, 24_000)

    def test_the_decoder_asks_for_mono_pcm_at_the_speaker_rate(self):
        command = decoder_command(22_050)
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-ar", command)
        self.assertEqual(command[command.index("-ar") + 1], "22050")
        self.assertEqual(command[command.index("-ac") + 1], "1")
        # Reading from and writing to pipes is what makes this a stream rather
        # than a decode-after-the-fact.
        self.assertIn("pipe:0", command)
        self.assertIn("pipe:1", command)


class AvailabilityTests(unittest.TestCase):
    def test_a_missing_ffmpeg_is_reported_as_itself(self):
        # The two dependencies have different fixes, so they are reported apart.
        with patch("athena.tts.edge.shutil.which", return_value=None):
            available, reason = edge_available()
        self.assertFalse(available)
        self.assertIn("ffmpeg", reason)

    def test_a_missing_edge_tts_is_reported_as_itself(self):
        with patch.dict("sys.modules", {"edge_tts": None}):
            with patch("athena.tts.edge.shutil.which", return_value="/usr/bin/ffmpeg"):
                available, reason = edge_available()
        self.assertFalse(available)
        self.assertIn("edge-tts", reason)


class _FakeEdge:
    """Stands in for `edge_tts.Communicate`, yielding MP3 in two chunks."""

    def __init__(self, payload=b"\xff\xfb\x00" * 40, fail: bool = False) -> None:
        self._payload = payload
        self._fail = fail

    async def stream(self):
        if self._fail:
            raise RuntimeError("edge service refused the connection")
        yield {"type": "audio", "data": self._payload}
        yield {"type": "WordBoundary", "data": b"ignored"}
        yield {"type": "audio", "data": self._payload}


class _FakeFfmpeg:
    """Stands in for the decoder process, emitting fixed PCM."""

    def __init__(self, pcm: bytes = b"\x01\x02" * 4096) -> None:
        self._pcm = pcm
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(pcm)
        self.stderr = _FakeStdout(b"")
        self.returncode = 0

    async def wait(self):
        return 0

    def kill(self):
        self.returncode = -9


class _FakeStdin:
    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


class _FakeStdout:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._done = False

    async def read(self, _size: int = -1) -> bytes:
        if self._done:
            return b""
        self._done = True
        return self._data


class TurnProtocolTests(unittest.IsolatedAsyncioTestCase):
    def synthesizer(self) -> EdgeSynthesizer:
        synth = EdgeSynthesizer()
        synth._loop = asyncio.get_running_loop()
        return synth

    def patched(self, *, fake_edge=None, fake_ffmpeg=None, fail_edge=False):
        """Patch the two external hops so the turn protocol can be driven offline.

        When Edge fails the decoder is given no PCM, because that is what really
        happens: no MP3 in means no audio out. A fake that emitted audio anyway
        would test a path that cannot occur.
        """
        edge = fake_edge or _FakeEdge(fail=fail_edge)
        ffmpeg = fake_ffmpeg or _FakeFfmpeg(pcm=b"" if fail_edge else b"\x01\x02" * 4096)

        async def spawn(*_args, **_kwargs):
            return ffmpeg

        modules = {"edge_tts": type("m", (), {"Communicate": lambda *a, **k: edge})}
        return patch.dict("sys.modules", modules), patch(
            "athena.tts.edge.asyncio.create_subprocess_exec", spawn)

    async def collect(self, synth, turn):
        return [chunk async for chunk in synth.audio(turn)]

    async def test_a_reply_comes_out_and_the_turn_ends(self):
        synth = self.synthesizer()
        turn = uuid4()
        p1, p2 = self.patched()
        with p1, p2:
            reader = asyncio.create_task(self.collect(synth, turn))
            await synth.send_text(turn, "Alarm set.")
            await synth.flush(turn)
            chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.turn_id == turn for chunk in chunks))
        self.assertEqual(b"".join(c.pcm for c in chunks), b"\x01\x02" * 4096)

    async def test_the_turn_ends_even_when_the_service_fails(self):
        # An unofficial API failing is the expected case, not the exotic one. A
        # turn that never ends is a service that stops speaking, so the sentinel
        # must be published on the failure path too.
        synth = self.synthesizer()
        turn = uuid4()
        p1, p2 = self.patched(fail_edge=True)
        with p1, p2:
            reader = asyncio.create_task(self.collect(synth, turn))
            await synth.send_text(turn, "Alarm set.")
            await synth.flush(turn)
            chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertEqual(chunks, [])

    async def test_cancel_ends_the_turn(self):
        synth = self.synthesizer()
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await asyncio.sleep(0)
        await synth.cancel(turn)
        self.assertEqual(await asyncio.wait_for(reader, timeout=5), [])

    async def test_the_first_byte_is_timed_for_the_log(self):
        synth = self.synthesizer()
        turn = uuid4()
        p1, p2 = self.patched()
        with p1, p2:
            reader = asyncio.create_task(self.collect(synth, turn))
            await synth.send_text(turn, "Alarm set.")
            await synth.flush(turn)
            await asyncio.wait_for(reader, timeout=5)
        self.assertGreaterEqual(synth.last_first_byte_ms, 0.0)
        self.assertGreaterEqual(synth.last_total_ms, 0.0)

    async def test_the_mp3_reaches_the_decoder(self):
        synth = self.synthesizer()
        turn = uuid4()
        ffmpeg = _FakeFfmpeg()
        p1, p2 = self.patched(fake_ffmpeg=ffmpeg)
        with p1, p2:
            reader = asyncio.create_task(self.collect(synth, turn))
            await synth.send_text(turn, "Alarm set.")
            await synth.flush(turn)
            await asyncio.wait_for(reader, timeout=5)
        # Both audio chunks, and the non-audio event filtered out.
        self.assertEqual(len(ffmpeg.stdin.written), 240)


class _BlockingEdge:
    """A Communicate that stalls before its first byte, like a slow connect."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def stream(self):
        self.entered.set()
        await self.gate.wait()
        yield {"type": "audio", "data": b"\xff\xfb\x00" * 40}


class PumpTests(unittest.IsolatedAsyncioTestCase):
    """The pump is the latency fix: `send_text` must never wait on synthesis.

    The old design made the coordinator's stream wait for each clause's Edge
    connection, so a three-clause answer fell apart between clauses. These
    tests pin the properties that keep conversation flowing.
    """

    def synthesizer(self) -> EdgeSynthesizer:
        synth = EdgeSynthesizer()
        synth._loop = asyncio.get_running_loop()
        return synth

    def patched(self, *, fake_edge=None, fake_ffmpeg=None):
        edge = fake_edge or _FakeEdge()
        ffmpeg = fake_ffmpeg or _FakeFfmpeg()

        async def spawn(*_args, **_kwargs):
            return ffmpeg

        modules = {"edge_tts": type("m", (), {"Communicate": lambda *a, **k: edge})}
        return patch.dict("sys.modules", modules), patch(
            "athena.tts.edge.asyncio.create_subprocess_exec", spawn)

    async def collect(self, synth, turn):
        return [chunk async for chunk in synth.audio(turn)]

    async def test_send_text_returns_while_edge_is_still_connecting(self):
        synth = self.synthesizer()
        turn = uuid4()
        edge = _BlockingEdge()
        p1, p2 = self.patched(fake_edge=edge)
        with p1, p2:
            await synth.send_text(turn, "Status report.")
            # send_text came back before the fake's first byte: the pump owns
            # the waiting, so the model's stream was never stalled behind it.
            await asyncio.wait_for(edge.entered.wait(), timeout=1)
            self.assertFalse(edge.gate.is_set())
            self.assertIsNotNone(synth._pump_task)
            edge.gate.set()
            reader = asyncio.create_task(self.collect(synth, turn))
            await synth.flush(turn)
            chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertTrue(chunks)

    async def test_clauses_share_one_decoder(self):
        synth = self.synthesizer()
        turn = uuid4()
        ffmpeg = _FakeFfmpeg()
        spawns: list[int] = []

        async def counting_spawn(*_args, **_kwargs):
            spawns.append(1)
            return ffmpeg

        p1, _ = self.patched(fake_ffmpeg=ffmpeg)
        with p1, patch("athena.tts.edge.asyncio.create_subprocess_exec",
                       counting_spawn):
            reader = asyncio.create_task(self.collect(synth, turn))
            await synth.send_text(turn, "First clause.")
            await synth.send_text(turn, "Second clause.")
            await synth.flush(turn)
            await asyncio.wait_for(reader, timeout=5)
        # One decoder for the whole turn — the per-clause spawn was ~0.7 s of
        # dead time between clauses on the board.
        self.assertEqual(len(spawns), 1)
        # And both clauses' MP3 went through it: two chunks per clause.
        self.assertEqual(len(ffmpeg.stdin.written), 480)

    async def test_connect_survives_a_failed_route_warm(self):
        synth = EdgeSynthesizer()

        async def refused(*_args, **_kwargs):
            raise OSError("network unreachable")

        with patch("athena.tts.edge.edge_available", return_value=(True, "")):
            with patch("athena.tts.edge.asyncio.open_connection", refused):
                await synth.connect()
        # The loop is still recorded: the warm-up is best effort, and a cold
        # route failing to warm must not take the service down with it.
        self.assertIsNotNone(synth._loop)


class BackendSelectionTests(unittest.TestCase):
    def test_asking_for_edge_uses_it_when_available(self):
        with patch("athena.tts.edge_available", return_value=(True, "")):
            with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "edge"}):
                self.assertIsInstance(build_synthesizer(_Settings()), EdgeSynthesizer)

    def test_asking_for_edge_falls_back_when_it_is_not(self):
        with patch("athena.tts.edge_available",
                   return_value=(False, "ffmpeg is not installed")):
            with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "edge"}):
                self.assertIsInstance(build_synthesizer(_Settings()),
                                      QwenRealtimeSynthesizer)

    def test_the_speaker_rate_follows_edge(self):
        with patch("athena.tts.edge_available", return_value=(True, "")):
            with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "edge"}):
                self.assertEqual(synthesizer_sample_rate(_Settings()), 24_000)

    def test_edge_is_free(self):
        # A price printed next to a free voice reads as though it is not free.
        with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "edge"}):
            self.assertFalse(speech_is_billed())

    def test_edge_is_not_called_local_in_the_log(self):
        # Free, but not local: it is Microsoft's servers. Calling it local would
        # be the same small lie as printing a price for it, and it matters when
        # diagnosing — a network voice going quiet has a different cause.
        with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "edge"}):
            self.assertEqual(speech_cost_label(), "Edge voice, free")
        with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "sherpa"}):
            self.assertEqual(speech_cost_label(), "local voice, free")


if __name__ == "__main__":
    unittest.main()
