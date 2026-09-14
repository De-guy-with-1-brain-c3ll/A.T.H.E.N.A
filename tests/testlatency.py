"""Measure streaming latency for the official DeepSeek API.

Examples:
    .venv/Scripts/python.exe testlatency.py
    .venv/Scripts/python.exe testlatency.py --runs 5
    .venv/Scripts/python.exe testlatency.py --prompt "What time is it?"

Set DEEPSEEK_API_KEY in the environment, or enter it securely when prompted.
"""

from __future__ import annotations

import argparse
import getpass
import os
import statistics
import sys
import time
from dataclasses import dataclass

from openai import OpenAI


DEFAULT_PROMPT = "Introduce yourself in one short sentence."
SYSTEM_PROMPT = "You are A.T.H.E.N.A Respond in one short sentence. No extra commentary. Speak like JARIS, Iron man's assistent"
MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class RunResult:
    first_text_seconds: float
    total_seconds: float
    characters: int


def percentile(values: list[float], percentage: float) -> float:
    """Return an interpolated percentile without extra dependencies."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * percentage
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def run_test(client: OpenAI, prompt: str, show_response: bool) -> RunResult:
    started = time.perf_counter()
    first_text_at: float | None = None
    response_parts: list[str] = []

    stream = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        stream=True,
        # Voice replies should be short. This also prevents an unexpectedly long
        # generation from delaying completion and consuming unnecessary tokens.
        max_tokens=40,
        temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},
    )

    for chunk in stream:
        text = chunk.choices[0].delta.content or ""
        if text:
            if first_text_at is None:
                first_text_at = time.perf_counter()
            response_parts.append(text)

    finished = time.perf_counter()
    response = "".join(response_parts).strip()

    if first_text_at is None:
        raise RuntimeError("DeepSeek returned no visible text.")

    if show_response:
        print(f"Response: {response}")

    return RunResult(
        first_text_seconds=first_text_at - started,
        total_seconds=finished - started,
        characters=len(response),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure DeepSeek V4 Flash streaming response latency."
    )
    parser.add_argument("--runs", type=int, default=3, help="Number of tests (default: 3)")
    parser.add_argument(
        "--warmups",
        type=int,
        default=1,
        help="Unmeasured connection/model warm-ups before testing (default: 1)",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt used for each test")
    parser.add_argument(
        "--hide-response",
        action="store_true",
        help="Do not print the model's response",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 2
    if args.warmups < 0:
        print("--warmups cannot be negative", file=sys.stderr)
        return 2

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        api_key = getpass.getpass("DeepSeek API key (input hidden): ").strip()
    if not api_key:
        print("No API key supplied.", file=sys.stderr)
        return 2

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=20.0,
        max_retries=0,
    )

    results: list[RunResult] = []
    print(
        f"\nTesting {MODEL} ({args.runs} measured run(s), "
        f"{args.warmups} warm-up, thinking disabled)\n"
    )

    try:
        for number in range(1, args.warmups + 1):
            print(f"Warm-up {number}/{args.warmups} (reported separately)")
            warmup = run_test(client, args.prompt, False)
            print(f"Warm-up first text: {warmup.first_text_seconds * 1000:.0f} ms")
            print(f"Warm-up completed:  {warmup.total_seconds * 1000:.0f} ms\n")

        for number in range(1, args.runs + 1):
            print(f"Run {number}/{args.runs}")
            result = run_test(client, args.prompt, not args.hide_response)
            results.append(result)
            print(f"First text: {result.first_text_seconds * 1000:.0f} ms")
            print(f"Completed:  {result.total_seconds * 1000:.0f} ms\n")
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"\nTest failed: {error}", file=sys.stderr)
        return 1
    finally:
        client.close()

    first_text = [result.first_text_seconds for result in results]
    totals = [result.total_seconds for result in results]

    print("Summary")
    print("-------")
    print(f"First text median: {statistics.median(first_text) * 1000:.0f} ms")
    print(f"First text p90:    {percentile(first_text, 0.90) * 1000:.0f} ms")
    print(f"Total median:      {statistics.median(totals) * 1000:.0f} ms")
    print(
        "\nThe measured runs represent an already-running ATHENA process with a "
        "reused network connection. Add STT, TTS, and playback latency to the "
        "first-text result."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
