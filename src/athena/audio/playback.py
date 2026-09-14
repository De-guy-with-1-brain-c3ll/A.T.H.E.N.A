from __future__ import annotations

import asyncio

import pyaudio


class Speaker:
    def __init__(self, sample_rate: int = 24_000, device: str | None = None) -> None:
        self._sample_rate = sample_rate
        self._device = device
        self._audio: pyaudio.PyAudio | None = None
        self._stream = None
        self._write_lock = asyncio.Lock()

    def _device_index(self) -> int | None:
        if not self._device:
            return None
        candidates = []
        wanted = self._device.casefold()
        for index in range(self._audio.get_device_count()):
            info = self._audio.get_device_info_by_index(index)
            if int(info.get("maxOutputChannels", 0)) <= 0:
                continue
            name = str(info.get("name", ""))
            if name.casefold() == wanted:
                return index
            if wanted in name.casefold():
                candidates.append(index)
        if candidates:
            return candidates[0]
        raise RuntimeError(f"Audio output device not found: {self._device}")

    async def open(self) -> None:
        self._audio = pyaudio.PyAudio()
        index = self._device_index()
        info = (self._audio.get_device_info_by_index(index) if index is not None
                else self._audio.get_default_output_device_info())
        print(f"Audio output: [{int(info['index'])}] {info['name']} at {self._sample_rate} Hz",
              flush=True)
        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._sample_rate,
            output=True,
            output_device_index=index,
        )

    async def play(self, pcm: bytes) -> None:
        if self._stream is not None:
            # PortAudio streams are not safe for concurrent writes. TTS and
            # background music share this stream, so serialize every write.
            async with self._write_lock:
                write = asyncio.create_task(asyncio.to_thread(self._stream.write, pcm))
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    await asyncio.gather(write, return_exceptions=True)
                    raise

    async def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.start_stream()

    async def close(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._audio is not None:
            self._audio.terminate()
            self._audio = None
