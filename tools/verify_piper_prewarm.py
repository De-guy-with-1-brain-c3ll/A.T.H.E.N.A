"""Does the coordinator's background warm-up actually remove the first wait?

The three-turn check (`verify_piper_warm.py`) proves the process is reused. This
proves the *first* reply benefits too, by warming the way the coordinator does —
in the background at start-up — and then timing a turn that begins after the
warm-up has had time to finish.

Run: ./.venv/Scripts/python.exe tools/verify_piper_prewarm.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

VOICE = ROOT / "outputs" / "voices" / "en_US-lessac-medium.onnx"


async def main() -> int:
    from athena.tts.piper import PiperSynthesizer

    binary = Path(sys.executable).parent / ("piper.exe" if sys.platform == "win32" else "piper")
    if not VOICE.is_file():
        print(f"no voice at {VOICE}")
        return 1

    speaker = PiperSynthesizer(voice=str(VOICE), binary=str(binary), sample_rate=22_050)
    await speaker.connect()

    # Exactly what the coordinator does at connect(): start it, do not wait.
    started = time.perf_counter()
    warm = asyncio.create_task(speaker.warm())
    print("start-up returned after %.3fs (warm-up is in the background)" % (time.perf_counter() - started))

    await warm
    print("voice loaded after %.3fs" % (time.perf_counter() - started))

    turn = uuid4()

    async def collect() -> tuple[float | None, int]:
        first = None
        size = 0
        async for chunk in speaker.audio(turn):
            if first is None:
                first = time.perf_counter()
            size += len(chunk.pcm)
        return first, size

    playback = asyncio.create_task(collect())
    before = time.perf_counter()
    await speaker.send_text(turn, "The meeting with CJ is at four thirty this afternoon.")
    await speaker.flush(turn)
    first, size = await playback
    await speaker.close()

    print(f"\nfirst reply of the session: first audio "
          f"{first - before:.3f}s ({size:,} bytes)")
    print("compare with a cold start, which pays the load here instead.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
