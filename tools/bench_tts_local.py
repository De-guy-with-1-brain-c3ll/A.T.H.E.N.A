"""Measure candidate local TTS engines the way ATHENA's turn loop would use them.

The question this answers is not "is the voice nice" but "can this board speak
faster than it plays, and how long before the first sound". Both figures decide
whether a local voice can replace the cloud one.

The important distinction, learned the hard way on Piper: a per-sentence number
that includes a model load is measuring the load, not the synthesis. So each
engine is measured twice — once cold (fresh process plus load) and once reused
(load already paid) — because only the reused figure describes a conversation.

    ./.venv/Scripts/python.exe tools/bench_tts_local.py
    ./.venv/Scripts/python.exe tools/bench_tts_local.py --models kitten kokoro piper
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

CANDIDATES = Path("outputs/tts-candidates")
PIPER_VOICES = Path("outputs/voices")

# A short reply and a long one, because the two stress different things: the
# short one is dominated by fixed overhead, the long one by throughput. Both are
# real ATHENA phrasings.
SENTENCES = {
    "short": "Alarm set.",
    "medium": "The meeting with CJ is at four thirty this afternoon.",
    "long": (
        "Good morning. You have three things today: the network meeting with CJ "
        "at four thirty, a Pre-Calculus assignment due Friday, and the thermostat "
        "is holding at seventy two degrees."
    ),
}


def read_rate(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        return handle.getframerate()


def write_wav(path: Path, samples, rate: int) -> None:
    import array
    path.parent.mkdir(parents=True, exist_ok=True)
    values = array.array("h")
    for sample in samples:
        clamped = max(-32768, min(32767, int(sample * 32767)))
        values.append(clamped)
    if sys.byteorder != "little":
        values.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(values.tobytes())


# ---------------------------------------------------------------- sherpa-onnx

def sherpa_config(model_dir: Path, threads: int):
    """Build the right config for whichever family the directory holds."""
    import sherpa_onnx

    tokens = str(model_dir / "tokens.txt")
    voice = model_dir / "model.int8.onnx"
    if not voice.is_file():
        voice = model_dir / "model.onnx"
    voices_bin = model_dir / "voices.bin"
    espeak = model_dir / "espeak-ng-data"

    name = model_dir.name
    if "kokoro" in name:
        model = sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(voice), tokens=tokens, voices=str(voices_bin)
                if voices_bin.is_file() else "",
                data_dir=str(espeak) if espeak.is_dir() else "",
            ),
            num_threads=threads, debug=False,
        )
    elif "kitten" in name:
        model = sherpa_onnx.OfflineTtsModelConfig(
            kitten=sherpa_onnx.OfflineTtsKittenModelConfig(
                model=str(voice), tokens=tokens, voices=str(voices_bin),
                data_dir=str(espeak) if espeak.is_dir() else "",
            ),
            num_threads=threads, debug=False,
        )
    elif "matcha" in name:
        model = sherpa_onnx.OfflineTtsModelConfig(
            matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                acoustic_model=str(voice), tokens=tokens,
                vocoder=str(model_dir / "vocos-22khz-univ.onnx"),
                data_dir=str(espeak) if espeak.is_dir() else "",
            ),
            num_threads=threads, debug=False,
        )
    else:
        raise SystemExit(f"unknown sherpa model family: {name}")
    return sherpa_onnx.OfflineTtsConfig(model=model)


def bench_sherpa(label: str, model_dir: Path, threads: int, repeats: int,
                 out_dir: Path) -> list[dict]:
    import sherpa_onnx

    print(f"\n=== {label} ({model_dir.name}) ===")
    if not model_dir.is_dir():
        print(f"  missing: {model_dir}")
        return []
    size = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
    print(f"  on-disk {size/1e6:.1f} MB")

    load_started = time.perf_counter()
    tts = sherpa_onnx.OfflineTts(sherpa_config(model_dir, threads))
    load = time.perf_counter() - load_started
    rate = tts.sample_rate
    print(f"  load {load:.2f}s   sample_rate {rate}   speakers {tts.num_speakers}")

    rows = []
    for name, text in SENTENCES.items():
        times, audio_seconds = [], 0.0
        first_audio = None
        for _ in range(repeats):
            started = time.perf_counter()
            audio = tts.generate(text, sid=0, speed=1.0)
            elapsed = time.perf_counter() - started
            if first_audio is None:
                first_audio = elapsed
            times.append(elapsed)
            audio_seconds = len(audio.samples) / rate
        best = min(times)
        med = statistics.median(times)
        write_wav(out_dir / f"{label}-{name}.wav", tts.generate(text, sid=0).samples, rate)
        rows.append({"engine": label, "sentence": name, "text": text,
                     "audio_s": audio_seconds, "load_s": load,
                     "best_s": best, "median_s": med,
                     "realtime_factor": audio_seconds / med, "sample_rate": rate})
        print(f"  {name:<7} audio {audio_seconds:>5.2f}s  "
              f"median {med:>6.2f}s  best {best:>6.2f}s  "
              f"{audio_seconds/med:>5.2f}x realtime")
    return rows


# ---------------------------------------------------------------------- piper

def bench_piper(label: str, voice: Path, threads: int, repeats: int,
                out_dir: Path) -> list[dict]:
    """Piper through its own binary: the baseline that must be beaten."""
    import subprocess

    print(f"\n=== {label} ({voice.name}) ===")
    if not voice.is_file():
        print(f"  missing: {voice}")
        return []
    print(f"  on-disk {voice.stat().st_size/1e6:.1f} MB")

    # Load time: one throwaway synthesis, which is what pays the voice load.
    load_started = time.perf_counter()
    subprocess.run(["piper", "--model", str(voice), "--output-raw"],
                   input=b"Hi.\n", stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    load = time.perf_counter() - load_started
    print(f"  cold start + load {load:.2f}s")

    rows = []
    for name, text in SENTENCES.items():
        times, audio_seconds = [], 0.0
        for _ in range(repeats):
            started = time.perf_counter()
            proc = subprocess.run(
                ["piper", "--model", str(voice), "--output-raw"],
                input=(text + "\n").encode(),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            times.append(time.perf_counter() - started)
            audio_seconds = len(proc.stdout) / 2 / 22050
        med = statistics.median(times)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{label}-{name}.raw").write_bytes(proc.stdout)
        rows.append({"engine": label, "sentence": name, "text": text,
                     "audio_s": audio_seconds, "load_s": load,
                     "best_s": min(times), "median_s": med,
                     "realtime_factor": audio_seconds / med, "sample_rate": 22050})
        print(f"  {name:<7} audio {audio_seconds:>5.2f}s  "
              f"median {med:>6.2f}s  {audio_seconds/med:>5.2f}x realtime  "
              f"(includes the {load:.1f}s load)")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="*",
                        default=["kitten", "kokoro", "piper"])
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", default="outputs/audio-tests/tts-local.json")
    parser.add_argument("--wavs", default="outputs/audio-tests/tts-local")
    args = parser.parse_args()

    out_dir = Path(args.wavs)
    rows: list[dict] = []

    print("=" * 78)
    print(f"LOCAL TTS — {args.threads} threads, median of {args.repeats}")
    print("=" * 78)

    for wanted in args.models:
        if wanted == "kitten":
            for name in ("kitten-nano-en-v0_8-int8", "kitten-micro-en-v0_8"):
                rows += bench_sherpa("kitten", CANDIDATES / name, args.threads,
                                     args.repeats, out_dir)
        elif wanted == "kokoro":
            rows += bench_sherpa("kokoro", CANDIDATES / "kokoro-int8-en-v0_19",
                                 args.threads, args.repeats, out_dir)
        elif wanted == "piper":
            rows += bench_piper("piper", PIPER_VOICES / "en_US-lessac-medium.onnx",
                                args.threads, args.repeats, out_dir)
        else:
            print(f"unknown model: {wanted}", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"threads": args.threads, "rows": rows}, indent=2))

    print()
    print("=" * 78)
    print(f"{'engine':<8} {'sentence':<8} {'audio':>7} {'median':>8} {'xRT':>7}")
    print("-" * 78)
    for row in rows:
        print(f"{row['engine']:<8} {row['sentence']:<8} {row['audio_s']:>6.2f}s "
              f"{row['median_s']:>7.2f}s {row['realtime_factor']:>6.2f}x")
    print()
    print(f"wavs -> {out_dir}")
    print(f"raw  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
