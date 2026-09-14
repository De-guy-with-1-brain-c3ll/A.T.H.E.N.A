r"""Stream microphone audio to Qwen Fun-ASR-Realtime.

Run from the project root:
    .venv\Scripts\python.exe tests\test_stt.py

Speak into the default microphone. Press Ctrl+C to finish.
Set DASHSCOPE_API_KEY, or enter the key securely when prompted.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading
import time


MODEL = "fun-asr-realtime"
SAMPLE_RATE = 16_000
FRAMES_PER_CHUNK = 1_600  # 100 ms of 16 kHz mono audio (3,200 bytes).


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test Qwen Fun-ASR-Realtime.")
    parser.add_argument(
        "--region",
        choices=("beijing", "singapore"),
        default="beijing",
        help="Model Studio region (default: beijing)",
    )
    parser.add_argument(
        "--semantic-punctuation",
        action="store_true",
        help="Enable semantic punctuation in partial results",
    )
    return parser.parse_args()


def api_key() -> str:
    value = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not value:
        value = getpass.getpass("DashScope API key (hidden): ").strip()
    if not value:
        raise ValueError("DASHSCOPE_API_KEY is required")
    return value


def main() -> int:
    args = parse_args()
    try:
        import dashscope
        import pyaudio
        from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult
    except ImportError as error:
        print(
            f"Missing dependency: {error.name}. Install with: "
            ".venv\\Scripts\\python.exe -m pip install -U dashscope pyaudio",
            file=sys.stderr,
        )
        return 2

    dashscope.api_key = api_key()
    dashscope.base_websocket_api_url = (
        "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
        if args.region == "beijing"
        else "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference"
    )

    microphone = None
    mic_stream = None
    callback_error: list[str] = []
    opened = threading.Event()
    started = time.perf_counter()
    first_result_at: float | None = None

    class Callback(RecognitionCallback):
        def on_open(self) -> None:
            opened.set()

        def on_close(self) -> None:
            pass

        def on_complete(self) -> None:
            print("Recognition completed.")

        def on_error(self, result: RecognitionResult) -> None:
            callback_error.append(str(result.message))
            opened.set()

        def on_event(self, result: RecognitionResult) -> None:
            nonlocal first_result_at
            sentence = result.get_sentence() or {}
            text = sentence.get("text", "").strip()
            if not text:
                return
            if first_result_at is None:
                first_result_at = time.perf_counter()
                print(f"First transcript: {(first_result_at - started) * 1000:.0f} ms")
            status = "FINAL" if RecognitionResult.is_sentence_end(sentence) else "PARTIAL"
            print(f"[{status}] {text}")

    recognition = Recognition(
        model=MODEL,
        format="pcm",
        sample_rate=SAMPLE_RATE,
        semantic_punctuation_enabled=args.semantic_punctuation,
        callback=Callback(),
    )

    try:
        recognition.start()
        opened.wait(timeout=10)
        if callback_error:
            raise RuntimeError(callback_error[0])
        if not opened.is_set():
            raise TimeoutError("Timed out while opening the recognition connection")

        microphone = pyaudio.PyAudio()
        mic_stream = microphone.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=FRAMES_PER_CHUNK,
        )
        print("Listening... Press Ctrl+C to stop.\n")
        while not callback_error:
            data = mic_stream.read(FRAMES_PER_CHUNK, exception_on_overflow=False)
            recognition.send_audio_frame(data)
        raise RuntimeError(callback_error[0])
    except KeyboardInterrupt:
        print("\nStopping recognition...")
        return 0
    except Exception as error:
        print(f"STT test failed: {error}", file=sys.stderr)
        return 1
    finally:
        if mic_stream is not None:
            mic_stream.stop_stream()
            mic_stream.close()
        if microphone is not None:
            microphone.terminate()
        try:
            recognition.stop()
        except Exception:
            pass
        print(
            "Metrics: request={}, first package={} ms, last package={} ms".format(
                recognition.get_last_request_id(),
                recognition.get_first_package_delay(),
                recognition.get_last_package_delay(),
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
