"""Minerva — OpenClaw-based voice agent framework.

Turn loop:
  STT (local, free) → OpenClaw (model + tools + memory) → TTS (local, free)

One turn active at a time. Interruptible.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

from minerva.config import settings
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
        self._openclaw_env = self._openclaw_environment()

    # ------------------------------------------------------------------ setup

    def _openclaw_environment(self) -> dict[str, str]:
        """Environment for OpenClaw subprocess calls.

        Reuses the same env every turn so credential lookups happen once.
        """
        env = os.environ.copy()
        env["OPENCLAW_NO_RESPAWN"] = "1"
        env["OPENCLAW_SKIP_CHANNELS"] = "1"
        return env

    async def connect(self) -> None:
        """Load STT and TTS models once, off the hot path."""
        await self.stt.connect()
        await self.tts.connect()

    # ------------------------------------------------------------------ turn loop

    async def run(self) -> None:
        """Main loop: listen → OpenClaw → speak → repeat."""
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

    # ------------------------------------------------------------------ stt

    async def _listen(self, turn_id: UUID) -> str:
        """Run STT until a final transcript arrives."""
        parts: list[str] = []
        await self.stt.start_turn(turn_id)
        try:
            async for result in self.stt.results():
                if result.is_final:
                    parts.append(result.text)
                    break
                parts.append(result.text)
        finally:
            await self.stt.finish_turn()
        return " ".join(parts).strip()

    # ------------------------------------------------------------------ openclaw

    async def _ask_openclaw(self, transcript: str) -> str:
        """Run one OpenClaw agent turn and return the reply text."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "openclaw",
                "infer",
                "model",
                "run",
                "--model",
                settings().model,
                "--prompt",
                transcript,
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._openclaw_env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=settings().openclaw_timeout_seconds
            )
        except (TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            return "(OpenClaw timed out)"

        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[:300]
            return f"(OpenClaw failed: {detail})"

        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            return "(OpenClaw returned no answer)"
        return text

    # ------------------------------------------------------------------ tts

    async def _speak(self, text: str) -> None:
        """Synthesize and play text via local TTS."""
        turn = self.active_turn or uuid4()
        await self.tts.connect()
        await self.tts.send_text(turn, text)
        await self.tts.flush(turn)
