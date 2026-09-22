"""Where does Piper's two seconds go, and what would a shared process save?

The per-turn benchmark (`bench_piper.py`) measured ~2.0 s to first audio because
ATHENA spawns a fresh Piper process for every reply. That number on its own does
not say *what* is slow, and the fix depends on the answer:

* if it is process start-up, a pool of idling processes helps;
* if it is loading the ONNX voice, the process must be started *before* the user
  finishes speaking, and a pool is the only thing that helps.

So this measures the stages separately, then measures one worker answering back
to back — the shape a pooled design would have.

Run: ./.venv/Scripts/python.exe tools/bench_piper_warm.py
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VOICES = ROOT / "outputs" / "voices"
OUT = ROOT / "outputs" / "audio-tests"

SENTENCES = {
    "plain": "The meeting with CJ is at four thirty this afternoon.",
    "long": ("Good morning. You have three things today: the network meeting with CJ at "
             "four thirty, a Pre-Calculus assignment due Friday, and the thermostat is "
             "holding at seventy two degrees. I have saved a brief for each."),
}
REPEATS = 3


def piper_binary() -> str:
    candidate = Path(sys.executable).parent / ("piper.exe" if sys.platform == "win32" else "piper")
    return str(candidate) if candidate.is_file() else "piper"


def voice_path(name: str) -> Path:
    return VOICES / f"{name}.onnx"


def spawn_and_time(binary: str, voice: Path, text: str) -> tuple[float, float, int]:
    """One-shot process, as ATHENA does it today. Returns (first, total, bytes)."""
    started = time.perf_counter()
    process = subprocess.Popen(
        [binary, "--model", str(voice), "--output-raw"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert process.stdin and process.stdout
    process.stdin.write((text + "\n").encode("utf-8"))
    process.stdin.close()
    first = 0.0
    total = 0
    while True:
        block = process.stdout.read(8192)
        if not block:
            break
        if first == 0.0:
            first = time.perf_counter() - started
        total += len(block)
    process.wait()
    return first, time.perf_counter() - started, total


def spawn_only(binary: str, voice: Path) -> float:
    """Cost of starting Piper and loading the voice, with nothing to say.

    An empty line makes it exit immediately, so this is start-up and model load
    with synthesis excluded.
    """
    started = time.perf_counter()
    process = subprocess.Popen(
        [binary, "--model", str(voice), "--output-raw"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert process.stdin and process.stdout
    process.stdin.write(b"\n")
    process.stdin.close()
    process.stdout.read()
    process.wait()
    return time.perf_counter() - started


def warm_worker(binary: str, voice: Path, turns: list[str]) -> list[dict]:
    """One process answering several lines, the shape a pool would have.

    The first line pays the load; every line after it should be pure synthesis.
    """
    process = subprocess.Popen(
        [binary, "--model", str(voice), "--output-raw"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert process.stdin and process.stdout
    rows: list[dict] = []
    for index, text in enumerate(turns):
        started = time.perf_counter()
        process.stdin.write((text + "\n").encode("utf-8"))
        process.stdin.flush()
        first = 0.0
        total = 0
        while total < 8192:
            block = process.stdout.read(8192)
            if not block:
                break
            if first == 0.0:
                first = time.perf_counter() - started
            total += len(block)
        rows.append({
            "turn": index + 1,
            "text": text[:40],
            "first_audio_s": round(first, 3),
            "first_chunk_s": round(time.perf_counter() - started, 3),
            "bytes": total,
        })
    process.stdin.close()
    process.stdout.read()
    process.wait()
    return rows


def main() -> int:
    binary = piper_binary()
    if not Path(binary).is_file() and binary == "piper":
        print("piper is not installed; nothing to measure")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    report: dict = {"binary": binary, "one_shot": [], "warm": {}}

    for name in ("en_US-lessac-medium", "en_US-amy-medium"):
        voice = voice_path(name)
        if not voice.is_file():
            continue

        loads = [spawn_only(binary, voice) for _ in range(REPEATS)]
        report.setdefault("startup_only_s", {})[name] = {
            "median": round(statistics.median(loads), 3),
            "min": round(min(loads), 3),
            "max": round(max(loads), 3),
            "n": len(loads),
        }
        print(f"{name}: start-up + voice load only -> median "
              f"{statistics.median(loads):.3f}s")

        for label, text in SENTENCES.items():
            samples = [spawn_and_time(binary, voice, text) for _ in range(REPEATS)]
            firsts = [s[0] for s in samples]
            report["one_shot"].append({
                "voice": name,
                "sentence": label,
                "first_median": round(statistics.median(firsts), 3),
                "first_min": round(min(firsts), 3),
                "total_median": round(statistics.median([s[1] for s in samples]), 3),
                "bytes": samples[0][2],
                "n": len(samples),
            })
            print(f"  one-shot {label:6s}: first audio median "
                  f"{statistics.median(firsts):.3f}s")

        turns = [SENTENCES["plain"], SENTENCES["plain"], SENTENCES["plain"],
                 SENTENCES["plain"], SENTENCES["plain"]]
        rows = warm_worker(binary, voice, turns)
        report["warm"][name] = rows
        print(f"  shared process, five identical turns:")
        for row in rows:
            print(f"    turn {row['turn']}: first audio {row['first_audio_s']:.3f}s "
                  f"({row['bytes']} bytes)")

    path = OUT / "piper-warm-results.json"
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nwritten -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
