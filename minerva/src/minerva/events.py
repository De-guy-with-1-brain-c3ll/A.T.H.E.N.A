from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Transcript:
    turn_id: UUID
    text: str
    is_final: bool
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class AudioChunk:
    turn_id: UUID
    pcm: bytes


@dataclass(frozen=True, slots=True)
class SpeechClause:
    turn_id: UUID
    text: str
