import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from minerva.core import MinervaCoordinator
from minerva.events import Transcript
from uuid import uuid4


class FakeRecognizer:
    def __init__(self, transcript="hello minerva"):
        self._transcript = transcript
        self.started = False
        self.finished = False

    async def connect(self):
        pass

    async def start_turn(self, turn_id):
        self.started = True

    async def results(self):
        yield Transcript(turn_id=uuid4(), text="hello ", is_final=False)
        yield Transcript(turn_id=uuid4(), text=self._transcript, is_final=True)

    async def finish_turn(self):
        self.finished = True

    async def close(self):
        pass


class FakeSynthesizer:
    def __init__(self):
        self.spoken = []

    async def connect(self):
        pass

    async def send_text(self, turn_id, text: str):
        self.spoken.append(text)

    async def flush(self, turn_id):
        pass

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_coordinator_loop_speaks_reply():
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
async def test_stt_final_transcript():
    coordinator = MinervaCoordinator()
    coordinator.stt = FakeRecognizer("the final answer")

    transcript = await coordinator._listen(uuid4())
    assert "the final answer" in transcript