"""Browser audio over the local network: framing, the bridge, and the dashboard."""
import asyncio
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from athena.remote_audio import (
    FRAME_BYTES,
    MAX_BACKLOG_FRAMES,
    BrowserMicrophone,
    BrowserSpeaker,
    RemoteAudio,
)
from athena.voice_ipc import (
    AUDIO_FRAME,
    FLUSH_FRAME,
    VOICE_OFFLINE,
    VoiceAudioServer,
    open_audio_stream,
    pack_frame,
    read_frame,
)
from athena.web import audio_stream
from athena.web_auth import COOKIE, SessionAuth


# asyncio can only serve Unix sockets on POSIX. Windows still runs every test
# here; the bridge is driven over loopback TCP, which exercises exactly the same
# framing and attach/detach code. The Unix socket itself is covered separately.
UNIX_STREAMS = hasattr(asyncio, "start_unix_server") and hasattr(socket, "AF_UNIX")


class BridgeHarness:
    """Runs the real VoiceAudioServer handler over loopback TCP."""

    def __init__(self, audio):
        self.bridge = VoiceAudioServer(audio)
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self.bridge._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def connect(self):
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None


class FakeSink:
    """Stands in for the socket or websocket carrying audio to the browser."""

    def __init__(self):
        self.closed = False
        self.binary = []
        self.controls = []

    async def send_bytes(self, pcm):
        self.binary.append(pcm)

    async def send_json(self, payload):
        self.controls.append(payload)


class RemoteAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_microphone_reframes_browser_audio_into_20ms_frames(self):
        audio = RemoteAudio()
        microphone = BrowserMicrophone(audio)
        audio.push(b"\x01\x02" * 700)  # 1400 bytes, not a whole number of frames
        frames = microphone.frames()
        try:
            first = await asyncio.wait_for(frames.__anext__(), 1)
            second = await asyncio.wait_for(frames.__anext__(), 1)
        finally:
            await frames.aclose()
        self.assertEqual(len(first), FRAME_BYTES)
        self.assertEqual(len(second), FRAME_BYTES)
        self.assertEqual(len(microphone._buffer), 1400 - 2 * FRAME_BYTES)

    async def test_backlog_is_bounded_so_latency_cannot_run_away(self):
        audio = RemoteAudio()
        for _ in range(MAX_BACKLOG_FRAMES + 40):
            audio.push(b"\x00" * FRAME_BYTES)
        self.assertLessEqual(audio._inbound.qsize(), MAX_BACKLOG_FRAMES)

    async def test_drain_discards_queued_microphone_audio(self):
        audio = RemoteAudio()
        audio.push(b"\x00" * FRAME_BYTES)
        audio.drain()
        self.assertEqual(audio._inbound.qsize(), 0)

    async def test_speaker_sends_pcm_and_flushes_for_interruption(self):
        audio = RemoteAudio()
        sink = FakeSink()
        audio.attach(sink)
        speaker = BrowserSpeaker(audio)
        await speaker.play(b"\x00\x01" * 10)
        await speaker.stop()
        self.assertEqual(sink.binary, [b"\x00\x01" * 10])
        self.assertEqual(sink.controls, [{"type": "flush"}])
        self.assertEqual(speaker._sample_rate, 24000)

    async def test_browser_speaker_paces_successive_packets(self):
        """A websocket write is instant; browser playback is not."""
        from unittest.mock import AsyncMock, patch

        audio = RemoteAudio()
        audio.attach(FakeSink())
        speaker = BrowserSpeaker(audio)
        pcm = b"\x00\x00" * 24_000  # exactly one second at 24 kHz/16-bit mono
        with patch("athena.remote_audio.asyncio.sleep", new=AsyncMock()) as sleep:
            await speaker.play(pcm)
            await speaker.play(pcm)
        sleep.assert_awaited_once()
        self.assertGreater(sleep.await_args.args[0], 0.9)

    async def test_nothing_is_sent_without_a_connected_device(self):
        speaker = BrowserSpeaker(RemoteAudio())
        await speaker.play(b"\x00\x01" * 10)  # must not raise
        await speaker.stop()
        await speaker.close()

    async def test_detaching_only_clears_the_matching_sink(self):
        audio = RemoteAudio()
        first, second = FakeSink(), FakeSink()
        audio.attach(first)
        self.assertTrue(audio.attached)
        audio.detach(second)
        self.assertTrue(audio.attached)
        audio.detach(first)
        self.assertFalse(audio.attached)

    async def test_a_closed_sink_counts_as_detached(self):
        audio = RemoteAudio()
        sink = FakeSink()
        audio.attach(sink)
        sink.closed = True
        self.assertFalse(audio.attached)
        await audio.send(b"\x00" * 10)  # must not raise or queue anything
        self.assertEqual(sink.binary, [])


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def _round_trip(self, frame_type, payload=b""):
        reader = asyncio.StreamReader()
        reader.feed_data(pack_frame(frame_type, payload))
        reader.feed_eof()
        return await read_frame(reader)

    async def test_audio_frame_round_trips(self):
        self.assertEqual(await self._round_trip(AUDIO_FRAME, b"pcm-bytes"),
                         (AUDIO_FRAME, b"pcm-bytes"))

    async def test_flush_frame_carries_no_payload(self):
        self.assertEqual(await self._round_trip(FLUSH_FRAME), (FLUSH_FRAME, b""))

    async def test_an_oversized_frame_is_refused(self):
        reader = asyncio.StreamReader()
        reader.feed_data(bytes([AUDIO_FRAME]) + (99 << 20).to_bytes(4, "big"))
        with self.assertRaises(ValueError):
            await read_frame(reader)


class VoiceAudioBridgeTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard relays browser audio to the voice process."""

    async def asyncSetUp(self):
        self.audio = RemoteAudio()
        self.harness = BridgeHarness(self.audio)
        await self.harness.start()
        self.addAsyncCleanup(self.harness.close)

    async def test_audio_travels_both_ways_through_the_bridge(self):
        reader, writer = await self.harness.connect()
        try:
            self.assertTrue(self.audio.attached)

            writer.write(pack_frame(AUDIO_FRAME, b"\x11\x22" * 20))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(self.audio.next_audio(), 2),
                             b"\x11\x22" * 20)

            await self.audio.send(b"\x33\x44" * 20)
            frame_type, payload = await asyncio.wait_for(read_frame(reader), 2)
            self.assertEqual((frame_type, payload), (AUDIO_FRAME, b"\x33\x44" * 20))

            await self.audio.flush()
            frame_type, _ = await asyncio.wait_for(read_frame(reader), 2)
            self.assertEqual(frame_type, FLUSH_FRAME)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def test_a_flush_frame_drains_queued_microphone_audio(self):
        reader, writer = await self.harness.connect()
        try:
            writer.write(pack_frame(AUDIO_FRAME, b"\x11\x22" * 20))
            await writer.drain()
            writer.write(pack_frame(FLUSH_FRAME))
            await writer.drain()
            for _ in range(80):
                await asyncio.sleep(0.02)
                if self.audio._inbound.qsize() == 0:
                    break
            self.assertEqual(self.audio._inbound.qsize(), 0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def test_the_voice_process_detaches_when_the_browser_leaves(self):
        reader, writer = await self.harness.connect()
        self.assertTrue(self.audio.attached)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        for _ in range(80):
            await asyncio.sleep(0.02)
            if not self.audio.attached:
                break
        self.assertFalse(self.audio.attached, "a departed device stayed attached")

    async def test_a_second_device_takes_over_from_the_first(self):
        reader, first = await self.harness.connect()
        reader2, second = await self.harness.connect()
        try:
            second.write(pack_frame(AUDIO_FRAME, b"\x55\x66" * 20))
            await second.drain()
            self.assertEqual(await asyncio.wait_for(self.audio.next_audio(), 2),
                             b"\x55\x66" * 20)
        finally:
            for writer in (first, second):
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def test_a_missing_voice_service_is_reported_not_hung(self):
        with self.assertRaises(RuntimeError) as caught:
            await open_audio_stream(Path(tempfile.gettempdir()) / "athena-not-there.sock")
        self.assertIn("voice is offline", str(caught.exception))


@unittest.skipUnless(UNIX_STREAMS, "asyncio needs POSIX for Unix sockets")
class UnixSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_real_unix_socket_carries_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice-audio.sock"
            audio = RemoteAudio()
            server = VoiceAudioServer(audio, path)
            await server.start()
            try:
                reader, writer = await open_audio_stream(path)
                try:
                    writer.write(pack_frame(AUDIO_FRAME, b"\xaa\xbb" * 20))
                    await writer.drain()
                    self.assertEqual(await asyncio.wait_for(audio.next_audio(), 2),
                                     b"\xaa\xbb" * 20)
                    await audio.send(b"\xcc\xdd" * 20)
                    frame_type, payload = await asyncio.wait_for(read_frame(reader), 2)
                    self.assertEqual((frame_type, payload),
                                     (AUDIO_FRAME, b"\xcc\xdd" * 20))
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
            finally:
                await server.close()
            self.assertFalse(path.exists(), "the socket file was left behind")


class BrowserAudioDetectionTests(unittest.IsolatedAsyncioTestCase):
    """The browser path must actually trigger the voice gate.

    This is the chain that decides whether ATHENA hears you at all: browser
    frames -> reframing -> energy gate. It is checked end to end because a
    framing or gating mistake here is invisible until someone speaks.
    """

    async def test_a_spoken_word_through_the_browser_opens_the_gate(self):
        from array import array

        from athena.audio.vad import VoiceGate

        def pcm_frame(amplitude, samples):
            return array("h", [amplitude] * samples).tobytes()

        samples = FRAME_BYTES // 2
        audio = RemoteAudio()
        microphone = BrowserMicrophone(audio)
        gate = VoiceGate(minimum_rms=400, noise_multiplier=2.7, start_ms=100)

        def push(amplitude, count):
            for _ in range(count):
                audio.push(pcm_frame(amplitude, samples))

        push(80, 10)       # a quiet room
        push(1200, 3)      # the word starts: three voiced frames, not yet enough
        push(220, 1)       # a quiet consonant mid-onset; the old code reset here
        push(1200, 10)     # and it carries on

        frames = microphone.frames()
        try:
            for _ in range(24):
                frame = await asyncio.wait_for(frames.__anext__(), 1)
                gate.process(frame)
                if gate.has_enough_speech:
                    break
        finally:
            await frames.aclose()

        self.assertTrue(gate.active, "browser audio never opened the gate")
        self.assertTrue(gate.has_enough_speech, "a spoken word was not accepted")

    async def test_a_silent_room_through_the_browser_never_opens_the_gate(self):
        from array import array

        from athena.audio.vad import VoiceGate

        audio = RemoteAudio()
        microphone = BrowserMicrophone(audio)
        gate = VoiceGate(minimum_rms=400, noise_multiplier=2.7, start_ms=100)
        for _ in range(30):
            audio.push(array("h", [70] * (FRAME_BYTES // 2)).tobytes())

        frames = microphone.frames()
        try:
            for _ in range(30):
                gate.process(await asyncio.wait_for(frames.__anext__(), 1))
        finally:
            await frames.aclose()
        self.assertFalse(gate.active, "a quiet room was mistaken for speech")


class StubState:
    """Only what the audio route asks the dashboard for."""

    def __init__(self, accepted=True):
        self.accepted = accepted
        self.password = "correct-horse-battery"
        self.auth = SessionAuth(self.password, b"s" * 40)

    def valid_session(self, token):
        return self.accepted and self.auth.valid(token)


class DashboardAudioRouteTests(unittest.IsolatedAsyncioTestCase):
    """The audio socket must be protected by the dashboard's own rules."""

    async def asyncSetUp(self):
        self.audio = RemoteAudio()
        self.harness = BridgeHarness(self.audio)
        self.state = StubState()
        app = web.Application()
        app["state"] = self.state
        app.router.add_get("/ws/audio", audio_stream)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        # Substitute the transport only: the route, the handler and the framing
        # under test are all the real ones.
        patcher = patch("athena.web.open_audio_stream", self.harness.connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sign_in(self, token=None):
        """Load a session cookie into the client jar.

        ws_connect takes no cookies argument, and aiohttp's default jar refuses
        cookies from a bare IP host, so the jar is used directly.
        """
        from yarl import URL
        self.client.session.cookie_jar.update_cookies(
            {COOKIE: token or self.state.auth.issue()},
            response_url=URL(str(self.client.make_url("/"))))

    async def test_the_socket_requires_a_dashboard_session(self):
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect("/ws/audio")

    async def test_a_forged_session_is_refused(self):
        self._sign_in(f"1.deadbeef.{'0' * 64}")
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect("/ws/audio")

    async def test_an_offline_voice_service_is_reported_to_the_browser(self):
        with patch("athena.web.open_audio_stream",
                   side_effect=RuntimeError(VOICE_OFFLINE)):
            self._sign_in()
            socket = await self.client.ws_connect("/ws/audio")
            message = await asyncio.wait_for(socket.receive_json(), 3)
            self.assertEqual(message["type"], "error")
            self.assertIn("voice is offline", message["message"])
            await socket.close()

    async def test_audio_flows_from_the_browser_to_the_voice_process(self):
        await self.harness.start()
        self.addAsyncCleanup(self.harness.close)
        self._sign_in()
        socket = await self.client.ws_connect("/ws/audio")
        ready = await asyncio.wait_for(socket.receive_json(), 3)
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["microphone_rate"], 16000)
        self.assertEqual(ready["speaker_rate"], 24000)

        await socket.send_bytes(b"\x11\x22" * 20)
        self.assertEqual(await asyncio.wait_for(self.audio.next_audio(), 3), b"\x11\x22" * 20)

        await self.audio.send(b"\x33\x44" * 20)
        message = await asyncio.wait_for(socket.receive(), 3)
        self.assertEqual(message.data, b"\x33\x44" * 20)

        await self.audio.flush()
        control = await asyncio.wait_for(socket.receive_json(), 3)
        self.assertEqual(control["type"], "flush")
        await socket.close()


class DashboardStaticTests(unittest.TestCase):
    """The control must live on the existing dashboard, not a second page."""

    def setUp(self):
        self.static = Path(__file__).resolve().parents[1] / "src" / "athena" / "web_static"

    def test_the_dashboard_page_carries_the_control(self):
        page = (self.static / "index.html").read_text(encoding="utf-8")
        for element in ("micStart", "micStop", "micMeter", "micStatus"):
            self.assertIn(f'id="{element}"', page)
        self.assertIn("Microphone and speaker", page)

    def test_the_script_connects_to_the_dashboard_audio_socket(self):
        script = (self.static / "app.js").read_text(encoding="utf-8")
        self.assertIn("/ws/audio", script)
        self.assertIn("getUserMedia", script)
        self.assertIn("athena-capture", script)
        # The level meter must show the real signal, never a placeholder.
        self.assertNotIn("Math.random()", script)
        # Automatic gain would move the level under the energy gate.
        self.assertIn("autoGainControl:false", script)

    def test_no_separate_audio_page_or_port_remains(self):
        module = self.static.parent / "remote_audio.py"
        self.assertFalse((self.static.parent / "remote_audio_page.html").exists())
        source = module.read_text(encoding="utf-8")
        self.assertNotIn("aiohttp", source, "browser audio must not open its own port")
        self.assertNotIn("tailscale", source.casefold())

    def test_nothing_references_tailscale_any_more(self):
        root = self.static.parents[2]
        for name in ("src/athena/remote_audio.py", "src/athena/main.py",
                     ".env.example", "orange_pi/config/athena.env.example",
                     "orange_pi/README.md"):
            with self.subTest(file=name):
                text = (root / name).read_text(encoding="utf-8")
                self.assertNotIn("tailscale", text.casefold())


if __name__ == "__main__":
    unittest.main()
