from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator

import pyaudio


class Microphone:
    """16-bit mono capture driven by a single dedicated reader thread.

    The previous design handed one frame to ``asyncio.to_thread`` at a time.
    Measured on this machine, that round trip costs ~150 us, and it is paid 50
    times a second, forever, whether or not anybody is talking — roughly 0.8%
    of a core purely on thread hand-off, and several times that on the ARM
    board this assistant actually runs on.

    One permanent reader thread also removes a whole class of failure.
    PortAudio is not safe against two concurrent readers, so every caller used
    to shield its read and wait for the outstanding one to finish before
    another could start. With one reader there is never an outstanding read to
    wait for: switching from listening to barge-in is instant instead of
    waiting up to one frame for a device read to return.
    """

    # Frames requested per device read. Larger reads mean fewer round trips;
    # this also bounds how long close() waits for the reader to notice the
    # stop flag, so it stays small enough to be imperceptible on shutdown.
    READ_BATCH = 5
    # Fresh-audio bound. Frames past this depth are dropped, so whatever a
    # consumer receives is recent even if nobody listened for a while.
    MAX_BUFFERED_FRAMES = 25

    def __init__(self, sample_rate: int = 16_000, frame_ms: int = 20,
                 device: str | None = None) -> None:
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._frames = max(1, sample_rate * frame_ms // 1000)
        self._frame_bytes = self._frames * 2
        self._device = device
        self._audio: pyaudio.PyAudio | None = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Written by the reader thread, drained by whichever consumer holds the
        # coordinator's microphone lock. deque append/popleft are thread safe.
        self._incoming: deque[bytes] = deque()
        self._ready = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dropped_frames = 0

    def _device_index(self) -> int | None:
        if not self._device:
            return None
        candidates = []
        wanted = self._device.casefold()
        for index in range(self._audio.get_device_count()):
            info = self._audio.get_device_info_by_index(index)
            if int(info.get("maxInputChannels", 0)) <= 0:
                continue
            name = str(info.get("name", ""))
            if name.casefold() == wanted:
                return index
            if wanted in name.casefold():
                candidates.append(index)
        if candidates:
            return candidates[0]
        raise RuntimeError(f"Audio input device not found: {self._device}")

    async def open(self) -> None:
        self._audio = pyaudio.PyAudio()
        index = self._device_index()
        info = (self._audio.get_device_info_by_index(index) if index is not None
                else self._audio.get_default_input_device_info())
        print(f"Audio input: [{int(info['index'])}] {info['name']} at {self._sample_rate} Hz",
              flush=True)
        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._sample_rate,
            input=True,
            input_device_index=index,
            frames_per_buffer=self._frames,
        )
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._incoming.clear()
        self._thread = threading.Thread(target=self._pump, name="microphone-reader",
                                        daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        """Read continuously so no consumer ever waits on the device."""
        stream = self._stream
        if stream is None:
            return
        batch = self._frames * self.READ_BATCH
        while not self._stop.is_set():
            try:
                data = stream.read(batch, exception_on_overflow=False)
            except Exception:
                # A dead or closed device has nothing left to offer.
                return
            if not data:
                continue
            # Keep frames sample-aligned even if the device returns a partial block.
            usable = len(data) - len(data) % self._frame_bytes
            for offset in range(0, usable, self._frame_bytes):
                if len(self._incoming) >= self.MAX_BUFFERED_FRAMES:
                    self._incoming.popleft()
                    self._dropped_frames += 1
                self._incoming.append(data[offset:offset + self._frame_bytes])
            loop = self._loop
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._ready.set)
                except RuntimeError:
                    return

    async def frames(self) -> AsyncIterator[bytes]:
        """Yield 20 ms frames as long as the caller keeps iterating.

        Only one consumer may iterate at a time; the coordinator's microphone
        lock enforces that, because two consumers would otherwise share the
        frames between them instead of each seeing every one.
        """
        if self._stream is None:
            raise RuntimeError("Microphone is not open")
        # Drop anything that accumulated while nobody was listening: that is
        # the loudspeaker's own tail, not something the user just said. The
        # newest frame keeps the pipeline warm without making the caller wait.
        while len(self._incoming) > 1:
            self._incoming.popleft()
        while True:
            while self._incoming:
                yield self._incoming.popleft()
            self._ready.clear()
            if self._incoming:
                continue
            await self._ready.wait()

    async def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            # The reader may be parked inside a device read; it notices the
            # stop flag once that read returns, which is at most one batch.
            await asyncio.to_thread(
                thread.join, max(1.0, self.READ_BATCH * self._frame_ms / 1000 * 4))
        # Nothing touches the stream except the reader, which has now stopped.
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if self._audio is not None:
            try:
                self._audio.terminate()
            except Exception:
                pass
            self._audio = None
        self._incoming.clear()
        self._ready.set()
