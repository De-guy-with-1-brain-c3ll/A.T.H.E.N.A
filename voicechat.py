"""First cloud voice-chat prototype for ATHENA.

Cloud STT/TTS: Azure Speech Free F0 tier
Conversation:   DeepSeek V4 Flash

Run:
    .venv/Scripts/python.exe voicechat.py

The program asks for missing credentials without displaying them. For regular
use, provide DEEPSEEK_API_KEY, AZURE_SPEECH_KEY, and AZURE_SPEECH_REGION as
environment variables instead.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from dataclasses import dataclass

import azure.cognitiveservices.speech as speechsdk
from openai import OpenAI


SYSTEM_PROMPT = """You are ATHENA, Benjamin's personal voice assistant.
Speak naturally, calmly, and concisely. Most replies should be one short
sentence. You are speaking aloud, so do not use Markdown, bullet points, URLs,
emoji, or stage directions. Never claim to have completed an action unless a
tool result confirms it."""


@dataclass(frozen=True)
class Settings:
    deepseek_key: str
    azure_key: str
    azure_region: str
    language: str
    voice: str
    warmup: bool


def secret(name: str, prompt: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        value = getpass.getpass(prompt).strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def load_settings(args: argparse.Namespace) -> Settings:
    region = os.environ.get("AZURE_SPEECH_REGION", "").strip()
    if not region:
        region = input("Azure Speech region (example: eastasia): ").strip()
    if not region:
        raise ValueError("AZURE_SPEECH_REGION is required")

    return Settings(
        deepseek_key=secret("DEEPSEEK_API_KEY", "DeepSeek API key (hidden): "),
        azure_key=secret("AZURE_SPEECH_KEY", "Azure Speech key (hidden): "),
        azure_region=region,
        language=args.language,
        voice=args.voice,
        warmup=not args.no_warmup,
    )


class AthenaVoiceChat:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

        self.deepseek = OpenAI(
            api_key=settings.deepseek_key,
            base_url="https://api.deepseek.com",
            timeout=20.0,
            max_retries=0,
        )

        speech_config = speechsdk.SpeechConfig(
            subscription=settings.azure_key,
            region=settings.azure_region,
        )
        speech_config.speech_recognition_language = settings.language
        speech_config.speech_synthesis_voice_name = settings.voice

        microphone = speechsdk.audio.AudioConfig(use_default_microphone=True)
        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=microphone,
        )
        # With no AudioConfig supplied, Azure plays through the default speaker.
        self.synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config,
        )

    def warm_up(self) -> None:
        started = time.perf_counter()
        response = self.deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": "Reply with only: Ready"},
                {"role": "user", "content": "Ready?"},
            ],
            stream=True,
            max_tokens=4,
            extra_body={"thinking": {"type": "disabled"}},
        )
        for _ in response:
            pass
        print(f"DeepSeek warmed in {(time.perf_counter() - started) * 1000:.0f} ms.")

    def listen(self) -> str | None:
        print("\nListening... Speak now.")
        result = self.recognizer.recognize_once_async().get()

        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            text = result.text.strip()
            if text:
                print(f"You: {text}")
                return text
            return None

        if result.reason == speechsdk.ResultReason.NoMatch:
            print("I did not understand that. Please try again.")
            return None

        if result.reason == speechsdk.ResultReason.Canceled:
            details = speechsdk.CancellationDetails(result)
            message = f"Speech recognition cancelled: {details.reason}"
            if details.error_details:
                message += f" ({details.error_details})"
            raise RuntimeError(message)

        return None

    def think(self, user_text: str) -> tuple[str, float, float]:
        self.messages.append({"role": "user", "content": user_text})
        # Keep the system prompt and the latest six exchanges. Long voice history
        # increases latency and is unnecessary for this first prototype.
        request_messages = [self.messages[0], *self.messages[-12:]]

        started = time.perf_counter()
        first_text_at: float | None = None
        parts: list[str] = []

        stream = self.deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=request_messages,
            stream=True,
            max_tokens=400,
            temperature=0.2,
            extra_body={"thinking": {"type": "disabled"}},
        )

        for chunk in stream:
            text = chunk.choices[0].delta.content or ""
            if not text:
                continue
            if first_text_at is None:
                first_text_at = time.perf_counter()
            parts.append(text)

        finished = time.perf_counter()
        answer = "".join(parts).strip()
        if not answer or first_text_at is None:
            raise RuntimeError("DeepSeek returned no visible response")

        self.messages.append({"role": "assistant", "content": answer})
        return answer, first_text_at - started, finished - started

    def speak(self, text: str) -> float:
        started = time.perf_counter()
        result = self.synthesizer.speak_text_async(text).get()
        elapsed = time.perf_counter() - started

        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            return elapsed

        if result.reason == speechsdk.ResultReason.Canceled:
            details = speechsdk.SpeechSynthesisCancellationDetails(result)
            message = f"Speech synthesis cancelled: {details.reason}"
            if details.error_details:
                message += f" ({details.error_details})"
            raise RuntimeError(message)

        raise RuntimeError(f"Unexpected speech result: {result.reason}")

    def run(self) -> None:
        if self.settings.warmup:
            print("Preparing ATHENA...")
            self.warm_up()

        print("\nATHENA is ready. Say 'goodbye' to exit, or press Ctrl+C.")

        while True:
            user_text = self.listen()
            if not user_text:
                continue

            normalized = user_text.casefold().strip(" .!?")
            if normalized in {"goodbye", "exit", "quit", "stop listening"}:
                farewell = "Goodbye, Benjamin."
                print(f"ATHENA: {farewell}")
                self.speak(farewell)
                return

            answer, first_text, model_total = self.think(user_text)
            print(f"ATHENA: {answer}")
            tts_total = self.speak(answer)
            print(
                "Latency: "
                f"DeepSeek first text {first_text * 1000:.0f} ms, "
                f"DeepSeek complete {model_total * 1000:.0f} ms, "
                f"TTS/playback complete {tts_total * 1000:.0f} ms"
            )

    def close(self) -> None:
        self.deepseek.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Talk with ATHENA using cloud speech.")
    parser.add_argument(
        "--language",
        default="en-US",
        help="Recognition language (default: en-US; Mandarin: zh-CN)",
    )
    parser.add_argument(
        "--voice",
        default="en-GB-RyanNeural",
        help="Azure neural voice name (default: en-GB-RyanNeural)",
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="Skip the small DeepSeek startup warm-up",
    )
    return parser.parse_args()


def main() -> int:
    try:
        settings = load_settings(parse_args())
        app = AthenaVoiceChat(settings)
        try:
            app.run()
        finally:
            app.close()
        return 0
    except KeyboardInterrupt:
        print("\nATHENA stopped.")
        return 130
    except Exception as error:
        print(f"\nATHENA could not start: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

