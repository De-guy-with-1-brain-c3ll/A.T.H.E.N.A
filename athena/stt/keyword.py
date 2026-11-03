"""Streaming local wake detection; only approved audio goes to Qwen STT."""
from __future__ import annotations

from array import array
import asyncio
import os
from pathlib import Path
import sys


def keyword_directory() -> Path:
    return Path(os.environ.get("ATHENA_KEYWORD_MODEL_DIR", "/opt/athena/models/keyword"))


class KeywordGate:
    streaming = True

    def __init__(self, directory: Path | None = None):
        self.directory = directory or keyword_directory()
        self._spotter = None
        self._stream = None
        self._pending = bytearray()

    async def connect(self):
        await asyncio.to_thread(self._load)

    def _load(self):
        import sherpa_onnx
        folder = self.directory
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(folder / "tokens.txt"),
            encoder=str(folder / "encoder.int8.onnx"),
            decoder=str(folder / "decoder.onnx"),
            joiner=str(folder / "joiner.int8.onnx"),
            keywords_file=str(folder / "keywords.txt"),
            num_threads=1, keywords_threshold=0.25,
        )
        self.reset()

    def reset(self):
        self._pending.clear()
        self._stream = self._spotter.create_stream() if self._spotter else None

    def _decode(self, pcm: bytes) -> bool:
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        self._stream.accept_waveform(16000, [sample / 32768 for sample in samples])
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            if self._spotter.get_result(self._stream):
                return True
        return False

    async def process(self, pcm: bytes, *, final: bool = False) -> bool:
        self._pending.extend(pcm)
        if not final and len(self._pending) < 2560:  # 80 ms, not 50 thread jobs/sec
            return False
        packet = bytes(self._pending)
        self._pending.clear()
        if final:
            packet += bytes(12800)  # Flush model lookahead, never sent to Qwen.
        # Join a cancelled decode before resetting/reusing its native stream.
        task = asyncio.create_task(asyncio.to_thread(self._decode, packet))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def close(self):
        self._stream = self._spotter = None


def keyword_available() -> bool:
    folder = keyword_directory()
    return all((folder / name).is_file() for name in (
        "tokens.txt", "keywords.txt", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx"))
