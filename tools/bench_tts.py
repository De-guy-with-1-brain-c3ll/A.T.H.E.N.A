"""Measure every cloud TTS model, voice and rate with real synthesis.

Synthesizes a known sentence, records the audio, and reports what it cost and how
long it took. Nothing here is simulated: these are real DashScope calls.

    ./.venv/Scripts/python.exe tools/bench_tts.py
    ./.venv/Scripts/python.exe tools/bench_tts.py --quick
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import statistics
import sys
import time
import wave
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from athena.config import load_local_environment

# A sentence chosen to contain the things that go wrong: a name, a code-switched
# word, numbers, and a hyphenated term.
SENTENCES = {
    "plain": "The meeting with CJ is at four thirty this afternoon.",
    "long": (
        "Good morning. You have three things today: the network meeting with CJ at "
        "four thirty, a Pre-Calculus assignment due Friday, and the thermostat is "
        "holding at seventy two degrees. I have saved a brief for each."
    ),
    "numbers": "The temperature is 71.4 degrees and the humidity is 58 percent.",
}

MODELS = [
    "qwen3-tts-flash-realtime",
]
VOICES = ["Neil", "Cherry", "Dolce"]
RATES = [1.0, 1.2]
SAMPLE_RATE = 24000

# Qwen realtime TTS is billed per 10,000 characters of input text.
CNY_PER_10K_CHARS = 1.0


@dataclass
class Result:
    model: str
    voice: str
    rate: float
    sentence: str
    chars: int
    seconds_to_first_audio: float = 0.0
    seconds_total: float = 0.0
    audio_bytes: int = 0
    audio_seconds: float = 0.0
    error: str = ""
    chunk_count: int = 0
    pcm: bytes = field(default=b"", repr=False)

    @property
    def realtime_factor(self) -> float:
        """Audio seconds produced per wall second spent. Higher is better."""
        if self.seconds_total <= 0 or self.audio_seconds <= 0:
            return 0.0
        return self.audio_seconds / self.seconds_total

    @property
    def cost_cny(self) -> float:
        return self.chars / 10_000 * CNY_PER_10K_CHARS


def write_wav(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


async def synthesize(model: str, voice: str, rate: float, text: str) -> Result:
    from athena.tts.qwen import QwenRealtimeSynthesizer

    result = Result(model=model, voice=voice, rate=rate,
                    sentence=text, chars=len(text))
    speaker = QwenRealtimeSynthesizer(
        os.environ["DASHSCOPE_API_KEY"], model, voice, settings=None)
    turn = uuid4()
    first_audio_at: float | None = None
    started = time.perf_counter()
    chunks: list[bytes] = []

    class Store:
        def get(self, key, default=None):
            return rate if key == "tts_speech_rate" else default

    speaker._settings = Store()
    try:
        await speaker.connect()
        await speaker.send_text(turn, text)
        await speaker.flush(turn)
        async for chunk in speaker.audio(turn):
            if first_audio_at is None:
                first_audio_at = time.perf_counter()
            chunks.append(chunk.pcm)
        result.seconds_total = time.perf_counter() - started
        result.seconds_to_first_audio = (
            (first_audio_at - started) if first_audio_at else result.seconds_total)
        result.pcm = b"".join(chunks)
        result.audio_bytes = len(result.pcm)
        result.chunk_count = len(chunks)
        # 16-bit mono
        result.audio_seconds = len(result.pcm) / 2 / SAMPLE_RATE
    except Exception as error:  # noqa: BLE001 - the point is to record the failure
        result.error = f"{type(error).__name__}: {error}"
        result.seconds_total = time.perf_counter() - started
    finally:
        try:
            await speaker.close()
        except Exception:
            pass
    return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="one sentence, one rate, to prove the path works")
    parser.add_argument("--out", default="outputs/audio-tests")
    args = parser.parse_args()

    load_local_environment()
    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("DASHSCOPE_API_KEY is not set.", file=sys.stderr)
        return 1

    out = Path(args.out)
    results: list[Result] = []

    if args.quick:
        combos = [("qwen3-tts-flash-realtime", "Dolce", 1.2, "plain")]
    else:
        combos = [(m, v, r, s)
                  for m in MODELS for v in VOICES for r in RATES for s in ("plain",)]

    print(f"Running {len(combos)} combination(s) against the live service.\n")
    for model, voice, rate, sent in combos:
        text = SENTENCES[sent]
        print(f"  {model} / {voice} / rate {rate} / {sent} ...", flush=True)
        result = await synthesize(model, voice, rate, text)
        results.append(result)
        if result.error:
            print(f"    FAILED: {result.error}")
        else:
            print(f"    first audio {result.seconds_to_first_audio:.2f}s, "
                  f"total {result.seconds_total:.2f}s, "
                  f"audio {result.audio_seconds:.2f}s "
                  f"({result.realtime_factor:.2f}x realtime), "
                  f"{result.chunk_count} chunks")
            path = out / model / f"{voice}-rate{rate}-{sent}.wav"
            write_wav(path, result.pcm)
            print(f"    saved -> {path}")

    # Also grab a real long-form sample and a numbers sample with the best voice.
    if not args.quick:
        for sent in ("long", "numbers"):
            text = SENTENCES[sent]
            print(f"  qwen3-tts-flash-realtime / Dolce / rate 1.2 / {sent} ...",
                  flush=True)
            result = await synthesize("qwen3-tts-flash-realtime", "Dolce", 1.2, text)
            results.append(result)
            if result.error:
                print(f"    FAILED: {result.error}")
            else:
                print(f"    first audio {result.seconds_to_first_audio:.2f}s, "
                      f"total {result.seconds_total:.2f}s, "
                      f"audio {result.audio_seconds:.2f}s")
                path = out / "qwen3-tts-flash-realtime" / f"Dolce-rate1.2-{sent}.wav"
                write_wav(path, result.pcm)
                print(f"    saved -> {path}")

    _report(results, out)
    return 0


def _report(results: list[Result], out: Path) -> None:
    good = [r for r in results if not r.error]
    bad = [r for r in results if r.error]

    print("\n" + "=" * 78)
    print("TTS RESULTS")
    print("=" * 78)
    header = f"{'model':<30} {'voice':<8} {'rate':>4} {'first':>7} {'total':>7} {'audio':>7} {'xRT':>6}"
    print(header)
    print("-" * 78)
    for r in good:
        print(f"{r.model:<30} {r.voice:<8} {r.rate:>4} "
              f"{r.seconds_to_first_audio:>6.2f}s {r.seconds_total:>6.2f}s "
              f"{r.audio_seconds:>6.2f}s {r.realtime_factor:>6.2f}")

    if good:
        print()
        firsts = [r.seconds_to_first_audio for r in good]
        print(f"time to first audio : median {statistics.median(firsts):.2f}s, "
              f"min {min(firsts):.2f}s, max {max(firsts):.2f}s")
        total_chars = sum(r.chars for r in good)
        total_cost = sum(r.cost_cny for r in good)
        print(f"this run cost       : {total_chars:,} characters = "
              f"CNY {total_cost:.4f}")

    if bad:
        print()
        print("FAILURES")
        for r in bad:
            print(f"  {r.model} / {r.voice} / {r.rate}: {r.error}")

    summary = {
        "results": [
            {"model": r.model, "voice": r.voice, "rate": r.rate, "sentence": r.sentence,
             "chars": r.chars, "first_audio_s": round(r.seconds_to_first_audio, 3),
             "total_s": round(r.seconds_total, 3),
             "audio_s": round(r.audio_seconds, 3),
             "realtime_factor": round(r.realtime_factor, 3),
             "chunks": r.chunk_count, "cost_cny": round(r.cost_cny, 5),
             "error": r.error}
            for r in results
        ]
    }
    path = out / "tts-results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2))
    print(f"\nraw results -> {path}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
