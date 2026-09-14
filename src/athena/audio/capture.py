from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pyaudio


class Microphone:
    def __init__(self, sample_rate: int = 16_000, frame_ms: int = 20,
                 device: str | None = None) -> None:
        self._sample_rate = sample_rate
        self._frames = sample_rate * frame_ms // 1000
        self._device = device
        self._audio: pyaudio.PyAudio | None = None
        self._stream = None

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

    async def frames(self) -> AsyncIterator[bytes]:
        if self._stream is None:
            raise RuntimeError("Microphone is not open")
        # Discard speaker echo buffered while listening was paused for a reply.
        available = self._stream.get_read_available()
        if available:
            await self._read(available)
        while True:
            yield await self._read(self._frames)

    async def _read(self, frames):
        read = asyncio.create_task(asyncio.to_thread(
            self._stream.read, frames, exception_on_overflow=False))
        try:
            return await asyncio.shield(read)
        except asyncio.CancelledError:
            # Finish the outstanding device read before starting another reader
            # or closing PortAudio. Usually at most one 20 ms frame.
            await asyncio.gather(read, return_exceptions=True)
            raise

    async def close(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._audio is not None:
            self._audio.terminate()
            self._audio = None
