"""Benchmark ATHENA's real voice path, stage by stage.

Run this on the Pi *with the VPN off* and with a short, naturally spoken WAV:

    python tools/bench_voice_pipeline.py --file /tmp/my-command.wav --runs 3

It deliberately uses the configured STT, DeepSeek model, and TTS backend, but
never supplies tools to the model, so the transcript cannot trigger an action.
The JSON report makes a later run comparable instead of relying on impressions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import time
import wave
from uuid import uuid4

def project_root() -> Path:
    """Find the checkout when this file is packaged as a Desktop executable."""
    candidates = [
        Path(os.environ["ATHENA_PROJECT_ROOT"])
        if os.environ.get("ATHENA_PROJECT_ROOT") else None,
        Path(__file__).resolve().parent.parent,
        Path.home() / "Desktop" / "VSCODE projects" / "ATHENA SOURCE",
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "src" / "athena").is_dir():
            return candidate
    raise RuntimeError("ATHENA SOURCE was not found. Set ATHENA_PROJECT_ROOT to its folder.")


ROOT = project_root()
sys.path.insert(0, str(ROOT / "src"))


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (1, 2, 16_000):
            raise ValueError("WAV must be 16 kHz, mono, 16-bit PCM (use sox/ffmpeg to convert it).")
        return handle.readframes(handle.getnframes())


def median(rows: list[dict], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    return round(statistics.median(values), 3) if values else None


def seconds(value: float | None) -> str:
    return f"{value:.3f}s" if value is not None else "none"


async def stt_turn(recognizer, pcm: bytes) -> dict:
    turn = uuid4()
    started = time.perf_counter()
    await recognizer.start_turn(turn)
    started_turn = time.perf_counter()
    first_partial = None
    final_at = None
    text = ""

    async def collect():
        nonlocal first_partial, final_at, text
        async for result in recognizer.results():
            if result.turn_id != turn:
                continue
            now = time.perf_counter()
            if first_partial is None and result.text and not result.text.startswith("[STT"):
                first_partial = now
            if result.is_final:
                final_at, text = now, result.text
                return

    listener = asyncio.create_task(collect())
    for offset in range(0, len(pcm), 3200):  # 100 ms, identical to ATHENA capture batches
        await recognizer.send_audio(pcm[offset:offset + 3200])
        await asyncio.sleep(0.1)
    audio_finished = time.perf_counter()
    await recognizer.finish_turn()
    await asyncio.wait_for(listener, timeout=15)
    if not text or text.startswith("[STT"):
        raise RuntimeError(f"STT returned {text or 'no final transcript'}")
    return {
        "text": text,
        "stt_start_s": round(started_turn - started, 3),
        "stt_first_partial_s": round(first_partial - started, 3) if first_partial else None,
        "stt_final_after_audio_s": round(final_at - audio_finished, 3),
    }


async def llm_and_tts(client, model: str, tts, transcript: str) -> dict:
    """Stream one harmless reply into the configured synthesizer."""
    from athena.llm.speech_chunker import SpeechChunker

    turn = uuid4()
    started = time.perf_counter()
    first_text = first_clause = first_pcm = None
    reply: list[str] = []

    async def collect_audio():
        nonlocal first_pcm
        total = 0
        async for chunk in tts.audio(turn):
            if first_pcm is None:
                first_pcm = time.perf_counter()
            total += len(chunk.pcm)
        return total

    audio = asyncio.create_task(collect_audio())
    request = {
        "model": model,
        "stream": True,
        "max_tokens": 64,
        "temperature": 0.2,
        "extra_body": {"thinking": {"type": "disabled"}},
        "messages": [
            {"role": "system", "content": "You are ATHENA. Reply in one short spoken sentence. Do not use tools."},
            {"role": "user", "content": transcript},
        ],
    }
    stream = await client.chat.completions.create(**request)
    chunker = SpeechChunker()
    try:
        async for event in stream:
            fragment = event.choices[0].delta.content or "" if event.choices else ""
            if not fragment:
                continue
            if first_text is None:
                first_text = time.perf_counter()
            reply.append(fragment)
            for clause in chunker.feed(fragment):
                if first_clause is None:
                    first_clause = time.perf_counter()
                await tts.send_text(turn, clause)
        for clause in chunker.finish():
            if first_clause is None:
                first_clause = time.perf_counter()
            await tts.send_text(turn, clause)
        await tts.flush(turn)
        bytes_out = await asyncio.wait_for(audio, timeout=45)
    finally:
        close = getattr(stream, "close", None)
        if close:
            await close()
        if not audio.done():
            audio.cancel()
            await asyncio.gather(audio, return_exceptions=True)
        await tts.cancel(turn)
    return {
        "reply": "".join(reply).strip(),
        "llm_first_text_s": round(first_text - started, 3) if first_text else None,
        "llm_first_clause_s": round(first_clause - started, 3) if first_clause else None,
        # Keep the end-to-end request-to-PCM metric, but also isolate actual
        # synthesis/transport after a clause was ready. The latter is what tells
        # an Edge/decoder delay apart from a model that simply had not reached
        # punctuation yet.
        "first_pcm_after_llm_s": round(first_pcm - started, 3) if first_pcm else None,
        "tts_first_pcm_after_clause_s": (
            round(first_pcm - first_clause, 3)
            if first_pcm is not None and first_clause is not None else None
        ),
        "tts_pcm_bytes": bytes_out,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Measure ATHENA STT → LLM → TTS latency.")
    parser.add_argument(
        "--file",
        default=str(ROOT / "outputs" / "audio-tests" / "stt-latency-current" / "stt-clips" / "cj.wav"),
        help="16 kHz mono PCM WAV of a real spoken command (defaults to the bundled CJ clip)",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", default="outputs/audio-tests/voice-pipeline.json")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    pcm = read_pcm(Path(args.file))

    from athena.config import Settings, load_local_environment
    from athena.settings.store import RuntimeSettingsStore
    from athena.stt import build_recognizer
    from athena.tts import build_synthesizer, tts_backend
    from openai import AsyncOpenAI

    load_local_environment()
    store = RuntimeSettingsStore()
    settings = Settings.from_environment(store)
    stt, tts = build_recognizer(settings), build_synthesizer(settings, store)
    client = AsyncOpenAI(api_key=settings.deepseek_api_key, base_url="https://api.deepseek.com", timeout=20, max_retries=0)
    rows = []
    try:
        warm_started = time.perf_counter()
        await stt.connect()
        stt_warm = time.perf_counter() - warm_started
        await tts.connect()
        warm = getattr(tts, "warm", None)
        if warm:
            await warm()
        print(f"STT ready in {stt_warm:.3f}s; TTS backend: {tts_backend()}")
        for index in range(args.runs):
            stt_result = await stt_turn(stt, pcm)
            response = await llm_and_tts(client, settings.deepseek_model, tts, stt_result["text"])
            row = {**stt_result, **response}
            rows.append(row)
            print(f"run {index + 1}: STT final {seconds(row['stt_final_after_audio_s'])} | "
                  f"LLM first {seconds(row['llm_first_text_s'])} | "
                  f"first clause {seconds(row['llm_first_clause_s'])} | "
                  f"TTS after clause {seconds(row['tts_first_pcm_after_clause_s'])} | "
                  f"end-to-end PCM {seconds(row['first_pcm_after_llm_s'])}")
    finally:
        await asyncio.gather(stt.close(), tts.close(), client.close(), return_exceptions=True)
    report = {"input": str(Path(args.file)), "runs": rows, "median": {
        "stt_final_after_audio_s": median(rows, "stt_final_after_audio_s"),
        "llm_first_text_s": median(rows, "llm_first_text_s"),
        "llm_first_clause_s": median(rows, "llm_first_clause_s"),
        "tts_first_pcm_after_clause_s": median(rows, "tts_first_pcm_after_clause_s"),
        "first_pcm_after_llm_s": median(rows, "first_pcm_after_llm_s"),
    }}
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("median:", report["median"])
    print("report ->", destination)
    # A console opened by double-clicking closes as soon as the benchmark
    # returns. Keep the result visible, but never block scripted invocations.
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        input("\nFinished. Press Enter to close.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
