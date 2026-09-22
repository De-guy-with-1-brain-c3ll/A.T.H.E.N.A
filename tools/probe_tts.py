"""Probe which TTS model names and voices the platform accepts, and check the
latency outliers by repeating each voice several times.

The first benchmark showed Dolce at rate 1.0 taking 3.45 s to first audio while the
same voice at 1.2 took 0.57 s. One sample cannot tell a slow voice from a cold
connection, so this repeats each combination and reports the spread.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from athena.config import load_local_environment

CANDIDATE_MODELS = [
    "qwen3-tts-flash-realtime",
    "qwen-tts-realtime",
    "qwen3-tts-flash",
    "qwen-tts",
    "cosyvoice-v2",
    "cosyvoice-v1",
]
CANDIDATE_VOICES = ["Neil", "Cherry", "Dolce", "Ethan", "Serena", "Chelsie"]
SENTENCE = "The meeting with CJ is at four thirty this afternoon."
REPEATS = 4
AUDIO_RATE = 24000
RESPONSE_TIMEOUT = 12.0


async def one(model: str, voice: str, rate: float = 1.2):
    from athena.tts.qwen import QwenRealtimeSynthesizer

    speaker = QwenRealtimeSynthesizer(
        os.environ["DASHSCOPE_API_KEY"], model, voice, settings=None)

    class Store:
        def get(self, key, default=None):
            return rate if key == "tts_speech_rate" else default

    speaker._settings = Store()
    turn = uuid4()
    started = time.perf_counter()
    first = None
    pcm = b""

    async def collect() -> None:
        nonlocal first, pcm
        async for chunk in speaker.audio(turn):
            if first is None:
                first = time.perf_counter() - started
            pcm += chunk.pcm

    try:
        await speaker.connect()
        await speaker.send_text(turn, SENTENCE)
        await speaker.flush(turn)
        # A model name the platform does not know is accepted at the socket, then
        # simply never emits an end-of-turn marker. Without this bound the probe
        # waits forever instead of reporting a rejection.
        await asyncio.wait_for(collect(), timeout=RESPONSE_TIMEOUT)
        total = time.perf_counter() - started
        return {"ok": True, "first": first, "total": total,
                "audio": len(pcm) / 2 / AUDIO_RATE, "pcm": pcm}
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"no end-of-turn within {RESPONSE_TIMEOUT:.0f}s",
                "total": time.perf_counter() - started,
                "audio": len(pcm) / 2 / AUDIO_RATE}
    except Exception as error:  # noqa: BLE001 - a rejected model is a result, not a crash
        return {"ok": False, "error": f"{type(error).__name__}: {error}",
                "total": time.perf_counter() - started}
    finally:
        try:
            await speaker.close()
        except Exception:
            pass


async def main() -> int:
    load_local_environment()
    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("DASHSCOPE_API_KEY is not set.", file=sys.stderr)
        return 1

    print("=" * 78)
    print("WHICH TTS MODEL NAMES WORK")
    print("=" * 78)
    accepted = []
    for model in CANDIDATE_MODELS:
        result = await one(model, "Neil")
        # A model name the platform does not know produces an empty stream rather
        # than a clean error, so "no audio" is the rejection signal here.
        if result["ok"] and result["audio"] > 0.3 and result["first"] is not None:
            print(f"  OK      {model:<28} first {result['first']:.2f}s, "
                  f"audio {result['audio']:.2f}s")
            accepted.append(model)
        else:
            reason = result.get("error") or (
                f"accepted but returned {result.get('audio', 0):.2f}s of audio, "
                f"no frames")
            if len(reason) > 90:
                reason = reason[:90] + "..."
            print(f"  reject  {model:<28} {reason}")

    print()
    print("=" * 78)
    print("WHICH VOICES WORK (on the first accepted model)")
    print("=" * 78)
    probe_model = accepted[0] if accepted else "qwen3-tts-flash-realtime"
    voices = []
    for voice in CANDIDATE_VOICES:
        result = await one(probe_model, voice)
        if result["ok"] and result["audio"] > 0.3:
            print(f"  OK      {voice:<12} audio {result['audio']:.2f}s")
            voices.append(voice)
        else:
            reason = result.get("error", f"only {result.get('audio', 0):.2f}s of audio")
            print(f"  reject  {voice:<12} {reason}")

    print()
    print("=" * 78)
    print(f"LATENCY SPREAD ({REPEATS} runs each, rate 1.2)")
    print("=" * 78)
    rows = []
    for voice in voices:
        firsts = []
        totals = []
        for _ in range(REPEATS):
            result = await one(probe_model, voice)
            if result["ok"]:
                firsts.append(result["first"])
                totals.append(result["total"])
        if not firsts:
            continue
        rows.append({
            "voice": voice,
            "first_median": statistics.median(firsts),
            "first_min": min(firsts),
            "first_max": max(firsts),
            "total_median": statistics.median(totals),
            "n": len(firsts),
        })
        print(f"  {voice:<12} first audio: median {statistics.median(firsts):.2f}s "
              f"(min {min(firsts):.2f}, max {max(firsts):.2f})  "
              f"total median {statistics.median(totals):.2f}s")

    out = Path("outputs/audio-tests/tts-probe.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"models_accepted": accepted, "voices_accepted": voices,
                               "latency": rows}, indent=2))
    print(f"\nraw results -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
