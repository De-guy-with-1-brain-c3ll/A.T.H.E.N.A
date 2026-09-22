"""Measure local STT latency the way a conversation actually experiences it.

`bench_sensevoice.py` answers "how fast is a decode" by handing the recogniser a
whole clip at once. That is not the question. ATHENA's real loop delivers audio
in 100 ms packets and re-decodes the growing buffer every 0.8 s to show partials,
so the numbers that matter are:

  * **time to first partial** — how long after you start speaking text appears
  * **partial update cost** — what each re-decode adds while you are still talking
  * **finalisation latency** — the gap between you stopping and the final text,
    which is the pause before ATHENA can begin to answer

That last one is the one a user feels, and it is invisible to a whole-clip
benchmark, because a whole-clip benchmark never pays for the partials.

This drives the real `SenseVoiceRecognizer` through the real protocol, so what is
measured is the code that ships, not a copy of its logic.

    ./.venv/Scripts/python.exe tools/bench_stt_latency.py
    ./.venv/Scripts/python.exe tools/bench_stt_latency.py --packet-ms 100 --threads 2

To test your own speech rather than the generated clips, point it at a file:

    ./.venv/Scripts/python.exe tools/bench_stt_latency.py --file my-speech.wav
    ./.venv/Scripts/python.exe tools/bench_stt_latency.py --record 5

`--record` grabs the microphone for N seconds and saves a 16 kHz mono clip under
`outputs/audio-tests/recordings/`, so a phrase that keeps failing can be captured
once and re-run against every recogniser afterwards.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time
import wave
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_stt import SENTENCES, word_error_rate  # noqa: E402

CLIPS = Path("outputs/audio-tests/stt-clips")
RECORDINGS = Path("outputs/audio-tests/recordings")
SAMPLE_RATE = 16_000
# ATHENA's capture loop delivers 100 ms packets; see the coordinator.
DEFAULT_PACKET_MS = 100


def read_pcm(path: Path) -> tuple[bytes, int]:
    """Read a 16 kHz mono 16-bit WAV. Anything else is converted, not rejected."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise SystemExit(f"{path} is {width * 8}-bit; 16-bit PCM is required.")
    if channels > 1:
        frames = _to_mono(frames, channels)
    if rate != SAMPLE_RATE:
        frames = resample(frames, rate, SAMPLE_RATE)
    return frames, SAMPLE_RATE


def _to_mono(pcm: bytes, channels: int) -> bytes:
    """Average the channels, so a stereo capture is usable rather than halved."""
    import array
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    mono = array.array("h", [0] * (len(samples) // channels))
    for index in range(len(mono)):
        total = 0
        for channel in range(channels):
            total += samples[index * channels + channel]
        mono[index] = int(total / channels)
    if sys.byteorder != "little":
        mono.byteswap()
    return mono.tobytes()


def resample(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """Linear resample, good enough for measurement.

    Deliberately not high quality: this exists so a 44.1 kHz phone recording can
    be measured without refusing it. Speech recognition is robust to the small
    artefacts a linear interpolation adds, and quantity of test material matters
    more here than the last ounce of fidelity.
    """
    import array
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    ratio = target_rate / source_rate
    count = int(len(samples) * ratio)
    out = array.array("h", [0] * count)
    for index in range(count):
        position = index / ratio
        low = int(position)
        high = min(low + 1, len(samples) - 1)
        weight = position - low
        out[index] = int(samples[low] * (1 - weight) + samples[high] * weight)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def record(seconds: float, out: Path) -> Path:
    """Capture the microphone to a 16 kHz mono clip."""
    try:
        import sounddevice  # noqa: PLC0415
    except ImportError:
        raise SystemExit(
            "Recording needs sounddevice:  ./.venv/Scripts/python.exe -m pip install sounddevice"
        ) from None
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Recording {seconds:.0f}s — speak now…")
    frames = sounddevice.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                             channels=1, dtype="int16")
    sounddevice.wait()
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(frames.tobytes())
    print(f"saved {out} ({seconds:.1f}s)")
    return out


def packetise(pcm: bytes, packet_ms: int) -> list[bytes]:
    """Split PCM into the packets the capture loop would hand over."""
    per_packet = int(SAMPLE_RATE * packet_ms / 1000) * 2  # 2 bytes per sample
    return [pcm[i:i + per_packet] for i in range(0, len(pcm), per_packet)]


async def run_turn(recognizer, pcm: bytes, packet_ms: int,
                   realtime: bool) -> dict:
    """Feed one clip through the recogniser exactly as the coordinator does.

    `realtime=False` sends the packets as fast as possible, which isolates the
    recogniser's own cost. `realtime=True` paces them to the clock, which is what
    actually happens when someone is speaking, and is the only way to see whether
    a partial decode can keep up.
    """
    packets = packetise(pcm, packet_ms)
    audio_seconds = len(pcm) / 2 / SAMPLE_RATE
    turn = uuid4()
    await recognizer.start_turn(turn)

    partial_times: list[float] = []
    partial_texts: list[str] = []
    first_partial: float | None = None
    started = time.perf_counter()

    # The recogniser publishes to its results() stream; collect concurrently.
    async def collect() -> None:
        nonlocal first_partial
        async for transcript in recognizer.results():
            if transcript.text == "[STT complete]":
                return
            if transcript.text.startswith("[STT error]"):
                return
            if transcript.text.strip():
                if first_partial is None:
                    first_partial = time.perf_counter() - started
                partial_times.append(time.perf_counter() - started)
                partial_texts.append(transcript.text)

    collector = asyncio.create_task(collect())

    for index, packet in enumerate(packets):
        if realtime:
            # Sleep until this packet would have been captured.
            due = started + (index + 1) * packet_ms / 1000
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        await recognizer.send_audio(packet)

    spoke_until = time.perf_counter()
    await recognizer.finish_turn()
    # Let the final transcript and the sentinel arrive.
    try:
        await asyncio.wait_for(collector, timeout=120)
    except asyncio.TimeoutError:
        collector.cancel()
    finalised = time.perf_counter()

    final_text = partial_texts[-1] if partial_texts else ""
    return {
        "audio_s": audio_seconds,
        "first_partial_s": first_partial,
        "partial_count": len(partial_texts),
        "finalise_s": finalised - spoke_until,
        "final_text": final_text,
        "partial_times": partial_times,
    }


async def report_file(recognizer, clip: Path, expectation: str | None,
                      repeats: int, packet_ms: int, plain: bool) -> dict:
    """Run one file through the real turn loop and print what came back."""
    pcm, _rate = read_pcm(clip)
    audio_seconds = len(pcm) / 2 / SAMPLE_RATE
    best = None
    for _ in range(max(1, repeats)):
        result = await run_turn(recognizer, pcm, packet_ms, realtime=True)
        if best is None or (result["first_partial_s"] or 9e9) < (best["first_partial_s"] or 9e9):
            best = result

    text = best["final_text"]
    if plain:
        # The useful shape when the point is "did it hear me correctly".
        print(f"  {clip.name} ({audio_seconds:.2f}s)")
        print(f'  -> "{text}"')
        if expectation:
            wer, missing = word_error_rate(expectation, text)
            print(f"     WER {wer:.2f}" + (f"  missing: {missing}" if missing else ""))
        print()
        return best

    first = best["first_partial_s"]
    wer_text = ""
    if expectation:
        wer, _ = word_error_rate(expectation, text)
        wer_text = f"  wer={wer:.2f}"
    print(f"  {clip.name}: {audio_seconds:.2f}s audio, "
          f"first partial {(f'{first:.2f}s' if first is not None else 'none')}, "
          f"{best['partial_count']} partials, "
          f"finalise {best['finalise_s']:.2f}s{wer_text}")
    print(f'  -> "{text}"')
    if expectation:
        print(f'     expected: "{expectation}"')
    print()
    return best


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-ms", type=int, default=DEFAULT_PACKET_MS)
    parser.add_argument("--threads", type=int, default=0,
                        help="0 keeps the configured default")
    parser.add_argument("--sentences", nargs="*", default=list(SENTENCES))
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--out", default="outputs/audio-tests/stt-latency.json")
    parser.add_argument("--file", nargs="*", default=None,
                        help="WAV file(s) or directories to transcribe instead of the clips")
    parser.add_argument("--expect", nargs="*", default=None,
                        help="ground-truth text per --file, for a WER score")
    parser.add_argument("--record", type=float, default=0.0,
                        help="record N seconds from the microphone and test it")
    parser.add_argument("--plain", action="store_true",
                        help="just print the transcript, no timing table")
    args = parser.parse_args()

    import os
    if args.threads:
        os.environ["ATHENA_SENSEVOICE_THREADS"] = str(args.threads)

    from athena.stt.sensevoice import SenseVoiceRecognizer, sensevoice_available

    available, reason = sensevoice_available()
    if not available:
        print(f"SenseVoice is not usable here: {reason}", file=sys.stderr)
        return 1

    recognizer = SenseVoiceRecognizer()
    print("=" * 78)
    print("LOCAL STT LATENCY — the real recogniser, driven like a live turn")
    print(f"packet size {args.packet_ms} ms, threads {args.threads or 'configured'}")
    print("=" * 78)

    # Unlike Piper, SenseVoice loads its model inside connect(), so there is no
    # separate warm-up and the load is paid once at start-up rather than on the
    # first turn. Time it, because on a Pi it is over ten seconds.
    load_started = time.perf_counter()
    await recognizer.connect()
    print(f"model load: {time.perf_counter() - load_started:.2f}s\n")

    # Your own speech is more informative than generated clips, so those modes
    # take priority and skip the sentence table entirely.
    if args.record:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        clip = record(args.record, RECORDINGS / f"rec-{stamp}.wav")
        await report_file(recognizer, clip, expectation=None,
                          repeats=args.repeats, packet_ms=args.packet_ms,
                          plain=args.plain)
        await recognizer.close()
        return 0

    if args.file:
        expectations = args.expect or []
        # A directory is expanded, so a phone's upload folder works directly.
        targets: list[Path] = []
        for raw in args.file:
            candidate = Path(raw)
            if candidate.is_dir():
                # Case-insensitive filesystems (Windows, macOS) match *.WAV for
                # *.wav too, so the two globs are merged rather than concatenated
                # or every file is reported twice.
                found = sorted({p.resolve() for p in candidate.iterdir()
                                if p.is_file() and p.suffix.lower() == ".wav"})
                if not found:
                    print(f"no .wav files in {candidate}", file=sys.stderr)
                targets.extend(found)
            elif candidate.is_file():
                targets.append(candidate)
            else:
                print(f"no such file: {candidate}", file=sys.stderr)
        if not targets:
            print("nothing to transcribe.", file=sys.stderr)
            await recognizer.close()
            return 1
        for index, clip in enumerate(targets):
            expected = expectations[index] if index < len(expectations) else None
            await report_file(recognizer, clip, expected, args.repeats,
                              args.packet_ms, args.plain)
        await recognizer.close()
        return 0

    rows = []
    print(f"{'clip':<10} {'audio':>6} {'1st partial':>11} {'partials':>8} "
          f"{'finalise':>9} {'wer':>5}  transcript")
    print("-" * 78)

    # Streamed: paced to the clock, so partial decodes compete with capture.
    for name in args.sentences:
        clip = CLIPS / f"{name}.wav"
        if not clip.is_file():
            print(f"  {name}: no clip at {clip}")
            continue
        pcm, _rate = read_pcm(clip)
        best = None
        for _ in range(args.repeats):
            result = await run_turn(recognizer, pcm, args.packet_ms, realtime=True)
            if best is None or (result["first_partial_s"] or 9e9) < (best["first_partial_s"] or 9e9):
                best = result
        wer, _ = word_error_rate(SENTENCES[name], best["final_text"])
        rows.append({"clip": name, **{k: v for k, v in best.items()
                                     if k != "partial_times"},
                     "wer": wer, "mode": "realtime"})
        first = best["first_partial_s"]
        print(f"{name:<10} {best['audio_s']:>5.2f}s "
              f"{(f'{first:.2f}s' if first is not None else '   none'):>11} "
              f"{best['partial_count']:>8} {best['finalise_s']:>8.2f}s "
              f"{wer:>5.2f}  {best['final_text'][:40]}")

    # Burst: no pacing, so the recogniser runs at full speed. The difference
    # against the paced column is how much the partial re-decodes cost.
    print()
    print("burst mode (packets sent instantly — isolates the recogniser's own cost):")
    burst_rows = []
    for name in args.sentences:
        clip = CLIPS / f"{name}.wav"
        if not clip.is_file():
            continue
        pcm, _rate = read_pcm(clip)
        started = time.perf_counter()
        result = await run_turn(recognizer, pcm, args.packet_ms, realtime=False)
        wall = time.perf_counter() - started
        rt_audio = result["audio_s"] / (result["first_partial_s"] or 9e9)
        burst_rows.append({"clip": name, "wall_s": wall, "audio_s": result["audio_s"],
                           "partials": result["partial_count"]})
        print(f"  {name:<10} audio {result['audio_s']:>5.2f}s  "
              f"wall {wall:>6.2f}s  partials {result['partial_count']:>3}  "
              f"{result['audio_s']/wall:>5.2f}x realtime")

    await recognizer.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"packet_ms": args.packet_ms,
                               "threads": args.threads or None,
                               "realtime": rows, "burst": burst_rows}, indent=2))

    finals = [r["first_partial_s"] for r in rows if r["first_partial_s"] is not None]
    finalise = [r["finalise_s"] for r in rows]
    print()
    if finals:
        print(f"median time to first partial: {statistics.median(finals):.2f}s")
    if finalise:
        print(f"median finalisation latency:  {statistics.median(finalise):.2f}s "
              f"(the pause before a reply can start)")
    print(f"\nraw results -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
