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
# Speech arrives synthesized at 24 kHz, and the tones and cached phrases are
# built at the same rate, so this stays the default for anything spoken.
SPEAKER_RATE = 24_000
SPEAKER_CHANNELS = 1
# Music is the one stream worth spending bandwidth on. The old pipeline ran
# everything at 24 kHz mono, which put a hard 12 kHz ceiling on every track and
# threw away the stereo image — that is what made it sound muffled. The browser
# can decode 48 kHz stereo as easily as anything else, so music gets its own
# format instead of being squeezed through the speech one.
MUSIC_RATE = 48_000
MUSIC_CHANNELS = 2
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
        self.dropped_frames = 0
        self.dropped_bytes = 0
        # What the attached browser says it can do itself. Empty until it
        # introduces itself, so nothing is ever assumed of a silent peer.
        self.browser_speech = False
        # Bumped on every attach. The speaker uses it to know that a *new*
        # browser needs to be told the audio format again, even when the format
        # itself has not changed since the last one.
        self.attach_generation = 0
        # Resolved by the browser when it has finished saying a reply out loud.
        self._speak_future: asyncio.Future | None = None

    @property
    def attached(self) -> bool:
        return self._sink is not None and not self._sink.closed

    def attach(self, sink: AudioSink) -> None:
        self._sink = sink
        self.connected.set()
        self.attach_generation += 1

    def detach(self, sink: AudioSink) -> None:
        if self._sink is sink:
            self._sink = None
            self.connected.clear()
            # A capability belongs to the browser that declared it, so it must
            # not outlive the connection. A turn waiting on that browser to
            # finish speaking must not wait forever either.
            self.browser_speech = False
            future, self._speak_future = self._speak_future, None
            if future is not None and not future.done():
                future.set_result(False)

    def expect_speech_done(self, future: asyncio.Future) -> None:
        """Register the future the browser's `speak_done` will resolve."""
        self._speak_future = future

    def control(self, payload: dict) -> None:
        """Take note of what the browser can do without ATHENA's help."""
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "capabilities":
            self.browser_speech = bool(payload.get("speech"))
        elif kind == "speak_done":
            future, self._speak_future = self._speak_future, None
            if future is not None and not future.done():
                future.set_result(payload.get("ok", True) is True)

    def push(self, pcm: bytes) -> None:
        """Microphone audio from the browser, dropping the oldest when behind."""
        while self._inbound.full():
            try:
                stale = self._inbound.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.dropped_frames += 1
            self.dropped_bytes += len(stale)
        try:
            self._inbound.put_nowait(pcm)
        except asyncio.QueueFull:
            self.dropped_frames += 1
            self.dropped_bytes += len(pcm)

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

    async def send_json(self, payload: dict) -> None:
        """Pass a control message to the browser, if one is attached."""
        sink = self._sink
        if sink is None or sink.closed:
            return
        try:
            await sink.send_json(payload)
        except (ConnectionResetError, RuntimeError, OSError):
            self.detach(sink)

    async def flush(self) -> None:
        """Tell the browser to stop queued playback, for immediate interruption."""
        await self.send_json({"type": "flush"})


class BrowserMicrophone:
    """Microphone frames supplied by the browser."""

    def __init__(self, audio: RemoteAudio, frame_bytes: int = FRAME_BYTES) -> None:
        self._audio = audio
        self._frame_bytes = frame_bytes
        self._buffer = bytearray()

    async def open(self) -> None:
        print("Browser audio: waiting for a device to connect on the dashboard.",
              flush=True)

    @property
    def dropped_frames(self) -> int:
        return self._audio.dropped_frames

    @property
    def dropped_seconds(self) -> float:
        return self._audio.dropped_bytes / (2.0 * MICROPHONE_RATE)

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

    def __init__(self, audio: RemoteAudio, sample_rate: int = SPEAKER_RATE,
                 channels: int = SPEAKER_CHANNELS) -> None:
        self._audio = audio
        self._sample_rate = sample_rate
        self._channels = channels
        self._next_play_time = 0.0
        self._generation = 0
        self._play_lock = asyncio.Lock()
        self._announced: tuple[int, int, int] | None = None

    @property
    def available(self) -> bool:
        """Whether a browser can currently receive playback.

        Music starts in the background, so reporting this before launching a
        decoder is much kinder than accepting a request that cannot make a
        sound because the dashboard page is not connected.
        """
        return self._audio.attached

    @property
    def format(self) -> tuple[int, int]:
        """The rate and channel count the last packet was sent with."""
        return self._sample_rate, self._channels

    @property
    def music_format(self) -> tuple[int, int]:
        """Music takes the full rate and both channels; speech keeps its own."""
        return MUSIC_RATE, MUSIC_CHANNELS

    @property
    def can_speak_text(self) -> bool:
        """Whether the attached browser has said it can speak for itself.

        False when nothing is connected, so the board falls back to making the
        sound itself rather than talking to an empty room.
        """
        return self._audio.attached and self._audio.browser_speech

    async def speak_text(self, text: str, timeout: float = 120.0) -> bool:
        """Ask the browser to say this itself instead of the board doing it.

        It is the same machine's speakers and microphone, so the turn has to
        stay open until the sound has actually stopped — hence the wait for the
        browser to report back, rather than a fire-and-forget message.
        """
        if not self.can_speak_text:
            return False
        done = asyncio.get_running_loop().create_future()
        self._audio.expect_speech_done(done)
        await self._audio.send_json({"type": "speak", "text": text})
        try:
            return bool(await asyncio.wait_for(done, timeout))
        except TimeoutError:
            print("The connected computer never finished speaking; stopping it.",
                  flush=True)
            await self._audio.send_json({"type": "speak_stop"})
            return False
        except asyncio.CancelledError:
            await self._audio.send_json({"type": "speak_stop"})
            raise

    async def _announce(self, rate: int, channels: int) -> None:
        """Tell the browser how to read the PCM that is about to arrive.

        Binary frames carry no header of their own, so the reader has to be
        told when the shape of the stream changes — and told again after a new
        browser connects, even when the shape is the same as the last one's.
        """
        marker = (self._audio.attach_generation, rate, channels)
        if self._announced == marker:
            return
        await self._audio.send_json({"type": "audio_format",
                                     "rate": rate, "channels": channels})
        self._announced = marker

    async def open(self) -> None:
        return None

    async def play(self, pcm: bytes, rate: int | None = None,
                   channels: int | None = None) -> None:
        """Play one packet, paced at the rate it will actually be heard.

        Every call states the format it is sending, because spoken replies
        (24 kHz mono) and music (48 kHz stereo) share this one stream.
        Declaring it per packet rather than per stream means neither can
        inherit the other's shape and come out at the wrong speed.
        """
        if not pcm:
            return
        rate = self._sample_rate if rate is None else int(rate)
        channels = self._channels if channels is None else int(channels)
        seconds = len(pcm) / (rate * channels * 2)
        generation = self._generation
        loop = asyncio.get_running_loop()
        async with self._play_lock:
            # Measured under the lock: an earlier packet may have held it for
            # the whole of its own playback, and pacing from a stale reading
            # would push this one late by that much again.
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
            await self._announce(rate, channels)
            await self._audio.send(pcm)
            self._next_play_time = max(self._next_play_time, loop.time()) + seconds

    async def finish_playback(self) -> None:
        """Hold speech ownership until the final scheduled packet is audible."""
        if not self._audio.attached:
            return
        generation = self._generation
        delay = self._next_play_time - asyncio.get_running_loop().time() + 0.06
        while delay > 0 and generation == self._generation:
            await asyncio.sleep(min(delay, 0.05))
            delay = self._next_play_time - asyncio.get_running_loop().time() + 0.06

    async def stop(self) -> None:
        # Do not wait on `_play_lock`: it may be sleeping to pace an old packet,
        # while an interruption needs the browser flush right now.
        self._generation += 1
        self._next_play_time = 0.0
        # Speech the browser is saying itself is not on this side of the socket,
        # so it needs cancelling explicitly as well as the PCM flush.
        await self._audio.send_json({"type": "speak_stop"})
        await self._audio.flush()

    async def close(self) -> None:
        return None
