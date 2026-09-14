from __future__ import annotations

import re


class SpeechChunker:
    """Turn streamed model fragments into short, speakable clauses."""

    _ending = re.compile(r"[.!?;](?:[\"')\]]+)?\s*$")

    def __init__(self, comma_threshold: int = 24, hard_limit: int = 70) -> None:
        self._buffer = ""
        self._comma_threshold = comma_threshold
        self._hard_limit = hard_limit

    def feed(self, fragment: str) -> list[str]:
        self._buffer += fragment
        clauses: list[str] = []
        while True:
            split_at = self._split_position()
            if split_at is None:
                break
            clause = self._buffer[:split_at].strip()
            self._buffer = self._buffer[split_at:].lstrip()
            if clause:
                clauses.append(clause)
        return clauses

    def finish(self) -> list[str]:
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if remainder else []

    def _split_position(self) -> int | None:
        for index, character in enumerate(self._buffer):
            if character in ".!?;" and index + 1 >= 2:
                return index + 1
            if character == "," and index + 1 >= self._comma_threshold:
                return index + 1
        if len(self._buffer) < self._hard_limit:
            return None
        boundary = self._buffer.rfind(" ", 0, self._hard_limit + 1)
        return boundary + 1 if boundary > 0 else self._hard_limit
