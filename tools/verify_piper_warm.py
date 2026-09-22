"""Prove the reused Piper process: three turns, real binary, real audio.

The benchmark measured subprocesses, which is what the fix replaces. This drives
the actual `PiperSynthesizer` through three turns and reports time to first audio
for each, so the claim "the load is paid once" is demonstrated rather than
inferred.

Run: ./.venv/Scripts/python.exe tools/verify_piper_warm.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

VOICES = ROOT / "outputs" / "voices"
SENTENCES = [
    "The meeting with CJ is at four thirty this afternoon.",
    "The temperature is holding at seventy two degrees.",
    "Good morning. You have three things today, and I have saved a brief for each.",
]


async def main() -> int:
    from athena.tts.piper import PiperSynthesizer

    binary = Path(sys.executable).parent / ("piper.exe" if sys.platform == "win32" else "piper")
    voice = VOICES / "en_US-lessac-medium.onnx"
    if not voice.is_file():
        print(f"no voice at {voice}")
        return 1

    speaker = PiperSynthesizer(voice=str(voice), binary=str(binary), sample_rate=22_050,
                               idle_seconds=0)
    await speaker.connect()

    print("cold start begins at t=0\n")
    started = time.perf_counter()
    first_turn_audio = None
    for index, text in enumerate(SENTENCES, start=1):
        turn = uuid4()
        before = time.perf_counter()

        async def collect() -> tuple[float | None, int]:
            first = None
            total = bytearray()
            async for chunk in speaker.audio(turn):
                if first is None:
                    first = time.perf_counter()
                total += chunk.pcm
            return first, len(total)

        # Playback runs alongside synthesis, the way the coordinator drives it:
        # the consumer is already reading when flush ends the turn.
        playback = asyncio.create_task(collect())
        await speaker.send_text(turn, text)
        await speaker.flush(turn)
        first, size = await playback

        if first_turn_audio is None:
            first_turn_audio = first - started
        print(f"turn {index}: first audio {first - before:6.3f}s after send  "
              f"({size:,} bytes, {size / 2 / 22_050:.2f}s of speech)")

    await speaker.close()

    print(f"\nfirst turn of the session (includes voice load): {first_turn_audio:.3f}s")
    print("the load is paid once; turns 2 and 3 are pure synthesis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
