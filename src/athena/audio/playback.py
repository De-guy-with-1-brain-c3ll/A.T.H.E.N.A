from __future__ import annotations

import asyncio
import sys
from array import array

import pyaudio


class Speaker:
    """Plays 16-bit mono PCM, with a volume the dashboard can move.

    Volume is applied here rather than by the mixer, because this is the one
    place every sound passes through: replies, cached phrases, alarm tones and
    music all call `play`. Scaling the samples keeps a single implementation
    and needs no `amixer`/`pactl`, which are not guaranteed to exist and would
    make the level apply only to whatever device they are pointed at.
    """

    def __init__(self, sample_rate: int = 24_000, device: str | None = None) -> None:
        self._sample_rate = sample_rate
        self._device = device
        self._audio: pyaudio.PyAudio | None = None
        self._stream = None
        self._write_lock = asyncio.Lock()
        # 1.0 means untouched, which is also what an unset environment gives.
        self._volume = 1.0
        # Scaling table for the current level, built once it is first needed
        # and dropped whenever the level changes. None means "not built yet".
        self._volume_table: list[int] | None = None

    @property
    def volume(self) -> int:
        """The output level as a whole percentage."""
        return round(self._volume * 100)

    def set_volume(self, percent: int) -> int:
        """Set the output level from a percentage, clamped to 0-100."""
        self._volume = max(0.0, min(1.0, int(percent) / 100.0))
        self._volume_table = None
        return self.volume

    def _apply_volume(self, pcm: bytes) -> bytes:
        if self._volume >= 0.999:
            return pcm
        table = self._volume_table
        if table is None:
            # One 65536-entry table per level turns the per-sample Python
            # arithmetic into a C-speed lookup. Music feeds ~10 chunks a
            # second, so the old per-sample float multiply was real work on
            # the Pi. entry[u] holds the two's-complement bit pattern of
            # int(sample * volume), with u the unsigned reading of that
            # sample — identical results to the loop it replaces.
            volume = self._volume
            table = [int((u if u < 32768 else u - 65536) * volume) % 65536
                     for u in range(65536)]
            self._volume_table = table
        samples = array("H")
        samples.frombytes(pcm)
        # The array module reads in machine order; PortAudio's 16-bit format is
        # little-endian, so a big-endian host must swap around the scaling.
        if sys.byteorder != "little":
            samples.byteswap()
        scaled = array("H", map(table.__getitem__, samples))
        if sys.byteorder != "little":
            scaled.byteswap()
        return scaled.tobytes()

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

    @property
    def music_format(self) -> tuple[int, int]:
        """What music should be decoded to for this device.

        The PortAudio stream below is opened once at a fixed rate and channel
        count, so the decoder is told to match it rather than the other way
        round. The USB device would accept 48 kHz stereo, but the stream is
        mono here, and re-opening it mid-track to change that would be a much
        larger change than the sound quality of the board's own little speaker
        justifies.
        """
        return self._sample_rate, 1

    async def play(self, pcm: bytes, rate: int | None = None,
                   channels: int | None = None) -> None:
        # A remote speaker can switch format per packet and uses those
        # arguments; this stream cannot, and is already opened at the format
        # that `music_format` reports.
        if self._stream is not None:
            pcm = self._apply_volume(pcm)
            # PortAudio streams are not safe for concurrent writes. TTS and
            # background music share this stream, so serialize every write.
            packet_bytes = max(2, self._sample_rate // 10 * 2)
            for offset in range(0, len(pcm), packet_bytes):
                async with self._write_lock:
                    write = asyncio.create_task(asyncio.to_thread(
                        self._stream.write, pcm[offset:offset + packet_bytes]))
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        await asyncio.gather(write, return_exceptions=True)
                        raise

    async def stop(self) -> None:
        if self._stream is not None:
            stream = self._stream
            # Pa_StopStream DRAINS the queued audio and blocks, so an
            # interruption would keep playing the old reply to its end while
            # the event loop froze with it. Pa_AbortStream discards whatever
            # is queued immediately — and since abort and restart are both
            # blocking PortAudio calls, they belong in a worker thread.
            def abort_and_restart():
                # PyAudio exposes abort only on its low-level PortAudio binding.
                pyaudio.pa.abort_stream(stream._stream)
                stream._is_running = False
                stream.start_stream()
            async with self._write_lock:
                await asyncio.to_thread(abort_and_restart)

    async def close(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._audio is not None:
            self._audio.terminate()
            self._audio = None
