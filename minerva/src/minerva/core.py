"""Minerva core coordinator.

Wires OpenClaw into a voice turn loop:

1. STT (local SenseVoice via sherpa-onnx, adapted from athena) turns audio
   into transcripts.
2. OpenClaw runs the agent turn (model + tools + memory).
3. TTS (local Piper via edge-tts fallback, adapted from athena) turns text
   into audio.

The coordinator keeps one turn active at a time and supports interruption.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from uuid import UUID, uuid4

from minerva.events import Transcript, AudioChunk
from minerva.stt import SenseVoiceRecognizer
from minerva.tts import PiperSynthesizer


class MinervaCoordinator:
    def __init__(self) -> None:
        self.state = "idle"  # idle | listening | thinking | speaking
        self.active_turn: UUID | None = None
        self.stt = SenseVoiceRecognizer()
        self.tts = PiperSynthesizer()
        self._listen_task: asyncio.Task | None = None

    async def connect(self) -> None:
        await self.stt.connect()
        await self.tts.connect()

    async def run(self) -> None:
        """Main turn loop: listen → OpenClaw → speak → repeat."""
        print("Minerva ready. Press Ctrl+C to stop.")
        try:
            while True:
                self.state = "listening"
                self.active_turn = uuid4()
                transcript = await self._listen(self.active_turn)
                if not transcript:
                    continue
                self.state = "thinking"
                reply = await self._ask_openclaw(transcript)
                self.state = "speaking"
                await self._speak(reply)
                self.state = "idle"
        except asyncio.CancelledError:
            raise
        finally:
            await self.stt.close()
            await self.tts.close()

    async def _listen(self, turn_id: UUID) -> str:
        """Run STT until a final transcript arrives, then return its text."""
        collected: list[str] = []
        await self.stt.start_turn(turn_id)
        try:
            async for result in self.stt.results():
                if result.is_final:
                    collected.append(result.text)
                    break
                collected.append(result.text)
        finally:
            await self.stt.finish_turn()
        return " ".join(collected).strip()

    async def _ask_openclaw(self, transcript: str) -> str:
        """Run one OpenClaw agent turn and return the reply text."""
        from athena.openclaw import run_task

        success, reply, meta = await run_task(
            objective=transcript,
            timeout=300,
            session="minerva-" + uuid4().hex,
        )
        if not success:
            return f"(OpenClaw did not confirm completion) {reply}"
        return reply

    async def _speak(self, text: str) -> None:
        """Synthesize and play text via local TTS."""
        await self.tts.connect()
        await self.tts.send_text(self.active_turn or uuid4(), text)
        await self.tts.flush(self.active_turn or uuid4())
