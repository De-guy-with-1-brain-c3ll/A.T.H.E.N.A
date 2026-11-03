"""Stand-ins for the cloud providers so ATHENA can be exercised locally.

No credentials, no cost, no network. Each fake implements the same duck-typed
surface the coordinator uses, so the real tool registry, alarm scheduler, tool
loop and voice gate are the production ones — only the paid edges are replaced.
"""
from __future__ import annotations

import asyncio
from array import array
from datetime import datetime, timezone
from uuid import UUID

from athena.events import AudioChunk, Transcript


def speech_frames(amplitude: int = 1400, frames: int = 12, samples: int = 320) -> list[bytes]:
    """Frames loud enough to open the voice gate."""
    return [array("h", [amplitude] * samples).tobytes() for _ in range(frames)]


def silence_frames(amplitude: int = 60, frames: int = 20, samples: int = 320) -> list[bytes]:
    return [array("h", [amplitude] * samples).tobytes() for _ in range(frames)]


class ScriptedMicrophone:
    """Feeds canned PCM frames instead of a sound card."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self._exhausted = asyncio.Event()

    async def open(self) -> None:
        print(f"Fake microphone: {len(self._frames)} frames queued.", flush=True)

    async def frames(self):
        for frame in self._frames:
            yield frame
        self._exhausted.set()
        # Behave like a silent room rather than ending the stream, so the
        # coordinator's listening turn simply waits instead of spinning.
        while True:
            await asyncio.sleep(0.02)
            yield silence_frames(1)[0]

    async def close(self) -> None:
        return None


class DiscardingSpeaker:
    """Accepts speech and throws it away, like a Pi with no sound hardware."""

    def __init__(self, sample_rate: int = 24_000) -> None:
        self._sample_rate = sample_rate
        self.played = bytearray()

    async def open(self) -> None:
        return None

    async def play(self, pcm: bytes) -> None:
        self.played.extend(pcm)

    async def stop(self) -> None:
        return None

    async def close(self) -> None:
        return None


class TranscriptRecognizer:
    """Returns a fixed transcript once it has been given enough audio."""

    def __init__(self, transcripts: list[str], min_audio_bytes: int = 1600) -> None:
        self._transcripts = list(transcripts)
        self._min_audio = min_audio_bytes
        self._received = 0
        self._turn: UUID | None = None
        self._results: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=50)
        self._finished = False

    async def connect(self) -> None:
        return None

    async def start_turn(self, turn_id: UUID) -> None:
        self._turn = turn_id
        self._received = 0
        self._finished = False

    async def send_audio(self, pcm: bytes) -> None:
        if self._finished or self._turn is None:
            return
        self._received += len(pcm)
        if self._received < self._min_audio:
            return
        if not self._transcripts:
            self._finished = True
            self._results.put_nowait(Transcript(self._turn, "[STT complete]", True, 1.0))
            return
        self._finished = True
        text = self._transcripts.pop(0)
        self._results.put_nowait(Transcript(self._turn, text, False, 0.9))
        self._results.put_nowait(Transcript(self._turn, text, True, 0.9))

    async def results(self):
        while True:
            yield await self._results.get()

    async def finish_turn(self) -> None:
        self._turn = None

    async def close(self) -> None:
        return None


class SilentSynthesizer:
    """Produces no audio but records what ATHENA would have said."""

    def __init__(self, sample_rate: int = 24_000) -> None:
        self._sample_rate = sample_rate
        self.spoken: list[str] = []
        self._turn: UUID | None = None
        self._audio: asyncio.Queue[AudioChunk | UUID] = asyncio.Queue()

    async def connect(self) -> None:
        return None

    async def send_text(self, turn_id: UUID, text: str) -> None:
        self._turn = turn_id
        self.spoken.append(text)
        print(f"   [speech] {text}", flush=True)
        self._audio.put_nowait(AudioChunk(turn_id, b"\x00\x00" * 240))

    async def audio(self, turn_id: UUID | None = None):
        wanted = turn_id or self._turn
        while True:
            item = await self._audio.get()
            if isinstance(item, UUID):
                if item == wanted:
                    return
            elif item.turn_id == wanted:
                yield item

    async def flush(self, turn_id: UUID) -> None:
        self._audio.put_nowait(turn_id)

    async def cancel(self, turn_id: UUID) -> None:
        self._audio.put_nowait(turn_id)

    async def close(self) -> None:
        return None


class OfflineLanguageModel:
    """Answers without calling any API, but runs the real local tool paths.

    ``stream_reply`` consults ``handle_user_command`` first, exactly like the
    production model does, so alarms, watches and the other local fast paths are
    the real implementations rather than a re-implementation.
    """

    def __init__(self, tools, replies: dict[str, str] | None = None) -> None:
        self._tools = tools
        self._replies = dict(replies or {})
        self._cancelled: set[UUID] = set()
        self.turns: list[str] = []

    @property
    def shutdown_requested(self) -> bool:
        return self._tools.shutdown_requested

    def is_confirmation_reply(self, text: str) -> bool:
        return self._tools.is_confirmation_reply(text)

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def cancel(self, turn_id: UUID) -> None:
        self._cancelled.add(turn_id)

    async def stream_reply(self, turn_id: UUID, text: str,
                           context_messages=None, *, on_connected=None):
        self.turns.append(text)
        if on_connected is not None:
            on_connected()
        direct = await self._tools.handle_user_command(text)
        if direct is not None:
            yield direct.spoken_text
            return
        if turn_id in self._cancelled:
            return
        yield self._replies.get(text, f"I heard: {text}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
