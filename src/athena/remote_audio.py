"""Browser microphone and speaker for ATHENA, over the local network.

ATHENA keeps running on the Orange Pi; the microphone and speaker can live in a
browser on a laptop, tablet or phone on the same network. Audio travels
browser -> dashboard -> voice service, so the dashboard's existing session and
its local-network-only rule protect it and no extra port is opened.

``BrowserMicrophone`` and ``BrowserSpeaker`` duck-type ``audio.capture.Microphone``
and ``audio.playback.Speaker``, so the voice coordinator needs no changes at all.
"""
from __future__ import annotations

import asyncio
from typing import Protocol


MICROPHONE_RATE = 16_000
SPEAKER_RATE = 24_000
FRAME_MS = 20
FRAME_BYTES = MICROPHONE_RATE * FRAME_MS // 1000 * 2
# One second of microphone audio. Beyond that the browser is ahead of ATHENA, so
# old frames are dropped instead of making every reply lag further behind.
MAX_BACKLOG_FRAMES = 50


class AudioSink(Protocol):
    """Whatever is currently carrying audio back to the browser."""

    @property
    def closed(self) -> bool: ...

    async def send_bytes(self, pcm: bytes) -> None: ...

    async def send_json(self, payload: dict) -> None: ...


class RemoteAudio:
    """Owns the single browser audio connection."""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MAX_BACKLOG_FRAMES)
        self._sink: AudioSink | None = None
        self.connected = asyncio.Event()

    @property
    def attached(self) -> bool:
        return self._sink is not None and not self._sink.closed

    def attach(self, sink: AudioSink) -> None:
        self._sink = sink
        self.connected.set()

    def detach(self, sink: AudioSink) -> None:
        if self._sink is sink:
            self._sink = None
            self.connected.clear()

    def push(self, pcm: bytes) -> None:
        """Microphone audio from the browser, dropping the oldest when behind."""
        while self._inbound.full():
            try:
                self._inbound.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._inbound.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    def drain(self) -> None:
        while not self._inbound.empty():
            try:
                self._inbound.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def next_audio(self) -> bytes:
        """Wait for the next microphone frame from the browser."""
        return await self._inbound.get()

    async def send(self, pcm: bytes) -> None:
        sink = self._sink
        if sink is None or sink.closed:
            return
        try:
            await sink.send_bytes(pcm)
        except (ConnectionResetError, RuntimeError, OSError):
            self.detach(sink)

    async def flush(self) -> None:
        """Tell the browser to stop queued playback, for immediate interruption."""
        sink = self._sink
        if sink is None or sink.closed:
            return
        try:
            await sink.send_json({"type": "flush"})
        except (ConnectionResetError, RuntimeError, OSError):
            self.detach(sink)


class BrowserMicrophone:
    """Microphone frames supplied by the browser."""

    def __init__(self, audio: RemoteAudio, frame_bytes: int = FRAME_BYTES) -> None:
        self._audio = audio
        self._frame_bytes = frame_bytes
        self._buffer = bytearray()

    async def open(self) -> None:
        print("Browser audio: waiting for a device to connect on the dashboard.",
              flush=True)

    async def frames(self):
        while True:
            self._buffer.extend(await self._audio.next_audio())
            while len(self._buffer) >= self._frame_bytes:
                frame = bytes(self._buffer[:self._frame_bytes])
                del self._buffer[:self._frame_bytes]
                yield frame

    async def close(self) -> None:
        self._buffer.clear()


class BrowserSpeaker:
    """Speech sent to the browser for playback at the speaker's real-time rate.

    A local PortAudio write blocks for the duration of the PCM it accepts.  A
    websocket write does not: Edge can synthesize a whole reply much faster than
    real time, and the browser's Web Audio scheduler then accumulates seconds of
    future audio.  The coordinator would reopen the microphone as soon as those
    socket writes finished, even though the browser was still speaking.  Pacing
    here restores the same contract as the physical speaker.
    """

    def __init__(self, audio: RemoteAudio, sample_rate: int = SPEAKER_RATE) -> None:
        self._audio = audio
        self._sample_rate = sample_rate
        self._next_play_time = 0.0
        self._generation = 0
        self._play_lock = asyncio.Lock()

    async def open(self) -> None:
        return None

    async def play(self, pcm: bytes) -> None:
        if not pcm:
            return
        generation = self._generation
        loop = asyncio.get_running_loop()
        async with self._play_lock:
            delay = self._next_play_time - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            # `stop` may have flushed the browser while this task was waiting.
            # Do not requeue a stale PCM packet after that flush.
            if generation != self._generation:
                return
            if not self._audio.attached:
                self._next_play_time = loop.time()
                return
            await self._audio.send(pcm)
            self._next_play_time = max(self._next_play_time, loop.time()) + (
                len(pcm) / (self._sample_rate * 2))

    async def stop(self) -> None:
        # Do not wait on `_play_lock`: it may be sleeping to pace an old packet,
        # while an interruption needs the browser flush right now.
        self._generation += 1
        self._next_play_time = 0.0
        await self._audio.flush()

    async def close(self) -> None:
        return None
