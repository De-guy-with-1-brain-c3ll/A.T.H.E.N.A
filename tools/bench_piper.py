"""Measure the local Piper backend for real: latency, realtime factor, and cost.

Piper is the free option, so the questions are whether it is fast enough and what
the latency actually is on real hardware. Published figures are not good enough to
choose a default on.

    ./.venv/Scripts/python.exe tools/bench_piper.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import time
import wave
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

VOICES_DIR = Path("outputs/voices").resolve()
SAMPLE_RATE = 22050
REPEATS = 5

# Piper installs its binary next to the interpreter that owns it. Relying on PATH
# is how "piper is not installed" appears on a machine that has piper installed.
def piper_binary() -> str:
    candidate = Path(sys.executable).parent / (
        "piper.exe" if os.name == "nt" else "piper")
    return str(candidate) if candidate.is_file() else "piper"

SENTENCES = {
    "plain": "The meeting with CJ is at four thirty this afternoon.",
    "long": (
        "Good morning. You have three things today: the network meeting with CJ at "
        "four thirty, a Pre-Calculus assignment due Friday, and the thermostat is "
        "holding at seventy two degrees. I have saved a brief for each."
    ),
    "numbers": "The temperature is 71.4 degrees and the humidity is 58 percent.",
}


def write_wav(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


async def synthesize(voice: str, text: str) -> dict:
    from athena.tts.piper import PiperSynthesizer

    model = VOICES_DIR / f"{voice}.onnx"
    speaker = PiperSynthesizer(voice=str(model), binary=piper_binary(),
                               sample_rate=SAMPLE_RATE)
    turn = uuid4()
    started = time.perf_counter()
    first = None
    pcm = b""
    try:
        await speaker.connect()
        await speaker.send_text(turn, text)
        await speaker.flush(turn)
        async for chunk in speaker.audio(turn):
            if first is None:
                first = time.perf_counter() - started
            pcm += chunk.pcm
        total = time.perf_counter() - started
        return {"ok": True, "first": first, "total": total,
                "audio": len(pcm) / 2 / SAMPLE_RATE, "pcm": pcm}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": f"{type(error).__name__}: {error}",
                "total": time.perf_counter() - started}
    finally:
        try:
            await speaker.close()
        except Exception:
            pass


async def main() -> int:
    voices = sorted(p.stem for p in VOICES_DIR.glob("*.onnx"))
    if not voices:
        print(f"No voices in {VOICES_DIR}. Run the download step first.", file=sys.stderr)
        return 1

    print("=" * 78)
    print("LOCAL PIPER — real synthesis, measured on this machine")
    print("=" * 78)
    print(f"voices: {', '.join(voices)}\n")

    rows = []
    out = Path("outputs/audio-tests/piper")

    for voice in voices:
        for label, text in SENTENCES.items():
            firsts, totals, audios = [], [], []
            for _ in range(REPEATS):
                result = await synthesize(voice, text)
                if not result["ok"]:
                    print(f"  {voice}/{label}: FAILED {result['error']}")
                    break
                firsts.append(result["first"])
                totals.append(result["total"])
                audios.append(result["audio"])
                last_pcm = result["pcm"]
            if not firsts:
                continue
            fastest_total = min(totals)
            audio = statistics.median(audios)
            # Realtime factor against the FASTEST observed run: that is the ceiling
            # the backend can reach once the model is warm.
            best_rtf = audio / fastest_total
            rows.append({
                "voice": voice, "sentence": label,
                "first_median": statistics.median(firsts),
                "first_min": min(firsts),
                "total_best": fastest_total,
                "audio_median": audio,
                "realtime_factor_best": best_rtf,
                "n": len(firsts),
            })
            print(f"  {voice:<24} {label:<8} first {statistics.median(firsts):.2f}s "
                  f"(best {min(firsts):.2f})  total best {fastest_total:.2f}s  "
                  f"audio {audio:.2f}s  {best_rtf:.2f}x realtime")
            write_wav(out / voice / f"{label}.wav", last_pcm)

    if rows:
        print()
        best = max(rows, key=lambda r: r["realtime_factor_best"])
        print(f"fastest realtime factor: {best['realtime_factor_best']:.2f}x "
              f"({best['voice']}, {best['sentence']})")
        firsts = [r["first_median"] for r in rows]
        print(f"time to first audio    : median {statistics.median(firsts):.2f}s "
              f"across all voices and sentences")
        print("cost per character     : 0 (runs locally, nothing is sent anywhere)")

    summary = Path("outputs/audio-tests/piper-results.json")
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps({"results": rows}, indent=2))
    print(f"\nraw results -> {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
