"""Minerva test suite.

Tests run without hardware — STT/TTS are stubbed so no microphone or
voice model is required.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from minerva.core import MinervaCoordinator
from minerva.events import Transcript


class FakeRecognizer:
    """STT that yields a partial then a final transcript."""

    def __init__(self, transcript: str = "hello minerva") -> None:
        self._transcript = transcript
        self.started = False
        self.finished = False

    async def connect(self) -> None:
        pass

    async def start_turn(self, turn_id) -> None:
        self.started = True

    async def results(self):
        yield Transcript(turn_id=uuid4(), text="partial ", is_final=False)
        yield Transcript(turn_id=uuid4(), text=self._transcript, is_final=True)

    async def finish_turn(self) -> None:
        self.finished = True

    async def close(self) -> None:
        pass


class FakeSynthesizer:
    """TTS that records what it was asked to speak."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def connect(self) -> None:
        pass

    async def send_text(self, turn_id, text: str) -> None:
        self.spoken.append(text)

    async def flush(self, turn_id) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_coordinator_loop_speaks_reply():
    """One turn: STT transcript → OpenClaw → TTS speaks reply."""
    coordinator = MinervaCoordinator()
    coordinator.stt = FakeRecognizer("turn the lights on")
    coordinator.tts = FakeSynthesizer()

    with patch("minerva.core.asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"I turned the lights on for you.", b"")
        )
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        coordinator.state = "listening"
        coordinator.active_turn = uuid4()
        transcript = await coordinator._listen(coordinator.active_turn)

        coordinator.state = "thinking"
        reply = await coordinator._ask_openclaw(transcript)
        assert reply == "I turned the lights on for you."

        coordinator.state = "speaking"
        await coordinator._speak(reply)
        assert coordinator.tts.spoken == ["I turned the lights on for you."]


@pytest.mark.asyncio
async def test_openclaw_failure_degrades():
    """OpenClaw failure returns an error message instead of crashing."""
    coordinator = MinervaCoordinator()
    coordinator.stt = FakeRecognizer()
    coordinator.tts = FakeSynthesizer()

    with patch("minerva.core.asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"connection refused"))
        mock_proc.returncode = 1
        mock_exec.return_value = mock_proc

        reply = await coordinator._ask_openclaw("test")
        assert "OpenClaw failed" in reply


@pytest.mark.asyncio
async def test_stt_final_transcript_extracted():
    """Final transcript is extracted from the results stream."""
    coordinator = MinervaCoordinator()
    coordinator.stt = FakeRecognizer("the final answer")

    transcript = await coordinator._listen(uuid4())
    assert "the final answer" in transcript