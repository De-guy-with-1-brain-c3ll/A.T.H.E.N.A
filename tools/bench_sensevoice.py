"""Measure SenseVoice-Small locally through sherpa-onnx.

This is the free option: no per-second billing and no network round trip. The
question is whether it is accurate enough, especially on the code-switched
sentence where the cloud recognisers drop CJ and the Chinese clause.

    ./.venv/Scripts/python.exe tools/bench_sensevoice.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_stt import SENTENCES, word_error_rate  # noqa: E402

MODEL_DIR = Path("outputs/models/sensevoice").resolve()
CLIPS = Path("outputs/audio-tests/stt-clips")
REPEATS = 3


def read_wav(path: Path) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    import array
    samples = array.array("h")
    samples.frombytes(frames)
    return [s / 32768.0 for s in samples], rate


def build(threads: int = 2, model: str = "model.int8.onnx"):
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(MODEL_DIR / model),
        tokens=str(MODEL_DIR / "tokens.txt"),
        num_threads=threads,
        use_itn=True,
        language="auto",
        debug=False,
    )


def main() -> int:
    if not (MODEL_DIR / "model.int8.onnx").is_file():
        print(f"Model not found in {MODEL_DIR}.", file=sys.stderr)
        return 1

    print("=" * 78)
    print("LOCAL SENSEVOICE via sherpa-onnx")
    print("=" * 78)

    # int8 is the realistic Pi choice (a third of the size, no GPU), so it is
    # measured against full precision to see whether the size is worth the
    # accuracy it might cost.
    variants = [("int8", "model.int8.onnx", 2),
                ("int8", "model.int8.onnx", 4)]
    if (MODEL_DIR / "model.onnx").is_file():
        variants.append(("fp32", "model.onnx", 2))

    all_rows = []
    for label, model_file, threads in variants:
        load_started = time.perf_counter()
        recognizer = build(threads, model_file)
        load_seconds = time.perf_counter() - load_started
        print(f"\n{label}  num_threads={threads}   model load: {load_seconds:.2f}s")
        print(f"{'clip':<10} {'wer':>6} {'audio':>7} {'decode':>8} {'xRT':>7}  transcript")
        print("-" * 78)

        rows = []
        for name in SENTENCES:
            clip = CLIPS / f"{name}.wav"
            if not clip.is_file():
                print(f"  {name}: no clip at {clip} (run bench_stt.py first)")
                continue
            samples, rate = read_wav(clip)
            audio_s = len(samples) / rate

            times = []
            text = ""
            for _ in range(REPEATS):
                stream = recognizer.create_stream()
                stream.accept_waveform(rate, samples)
                started = time.perf_counter()
                recognizer.decode_stream(stream)
                times.append(time.perf_counter() - started)
                text = stream.result.text

            best = min(times)
            rtf = audio_s / best if best else 0.0
            wer, _missing = word_error_rate(SENTENCES[name], text)
            rows.append({"clip": name, "wer": wer, "audio_s": audio_s,
                         "decode_best_s": best, "realtime_factor": rtf,
                         "text": text, "threads": threads, "precision": label})
            print(f"{name:<10} {wer:>6.2f} {audio_s:>6.2f}s {best:>7.3f}s "
                  f"{rtf:>7.2f}  {text[:52]}")

        if rows:
            mean_wer = sum(r["wer"] for r in rows) / len(rows)
            mean_rtf = sum(r["realtime_factor"] for r in rows) / len(rows)
            print(f"\n  mean WER {mean_wer:.3f}   mean {mean_rtf:.2f}x realtime "
                  f"(higher is faster than realtime)")
            all_rows.extend(rows)

    out = Path("outputs/audio-tests/sensevoice-results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": "sensevoice-small", "rows": all_rows},
                              indent=2))
    print(f"\nraw results -> {out}")
    print("\ncost: 0 per hour — runs locally, nothing is uploaded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
