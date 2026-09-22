"""Run ATHENA locally with fake providers, so behaviour can be tested without
the Orange Pi, without cloud credentials, and without spending anything.

The real tool registry, alarm scheduler, voice gate and tool loop are all the
production ones. Only the paid edges (speech recognition, the language model,
speech synthesis) are replaced, and everything runs against a throwaway data
directory so your real alarms and memory are untouched.

    python -m athena.dev.harness alarms     # speak an alarm and watch it fire
    python -m athena.dev.harness vad        # show the gate's decisions frame by frame
    python -m athena.dev.harness chat       # type to ATHENA, with live alarms
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

from athena.audio.vad import VoiceGate
from athena.coordinator import VoiceCoordinator
from athena.dev.fakes import (
    DiscardingSpeaker,
    OfflineLanguageModel,
    ScriptedMicrophone,
    SilentSynthesizer,
    TranscriptRecognizer,
    silence_frames,
    speech_frames,
)
from athena.memory.service import MemoryService
from athena.services import build_registry
from athena.settings.store import RuntimeSettingsStore


def sandbox_environment(directory: Path) -> None:
    """Point every store at a throwaway directory, never the real one."""
    os.environ["ATHENA_DATA_DIR"] = str(directory)
    os.environ["ATHENA_DATABASE_PATH"] = str(directory / "athena.db")
    os.environ.setdefault("ATHENA_TIMEZONE", "Asia/Shanghai")
    os.environ.setdefault("ATHENA_WAKE_WORD", "athena")
    # Never try to reach a model while testing.
    os.environ.pop("MICROSOFT_CLIENT_ID", None)


def build_settings(directory: Path) -> RuntimeSettingsStore:
    store = RuntimeSettingsStore(directory / "settings.json")
    # Memory summaries would call the real API; the harness stays offline.
    store.set("memory_enabled", False)
    return store


async def scenario_alarms(utterance: str, wait_seconds: float) -> int:
    """Speak an alarm through the whole voice path and watch it actually fire."""
    print(f"Speaking: {utterance!r}")
    microphone = ScriptedMicrophone(speech_frames() + silence_frames(60))
    speaker = DiscardingSpeaker()
    stt = TranscriptRecognizer([utterance])
    tts = SilentSynthesizer()
    with tempfile.TemporaryDirectory(prefix="athena-harness-") as directory:
        root = Path(directory)
        sandbox_environment(root)
        settings = build_settings(root)
        registry, alerts = build_registry(settings)
        llm = OfflineLanguageModel(registry)
        coordinator = VoiceCoordinator(
            microphone=microphone,
            speaker=speaker,
            stt=stt,
            llm=llm,
            tts=tts,
            memory=MemoryService(root / "athena.db", "offline", "offline",
                                 settings.get("memory_batch_delay_seconds"), settings),
            voice_gate=VoiceGate(minimum_rms=400, noise_multiplier=2.7,
                                 end_silence_ms=260),
            settings_store=settings,
        )
        alerts.notify = coordinator.enqueue_external_speech
        await coordinator.connect()
        await alerts.start()
        run = asyncio.create_task(coordinator.run())
        try:
            await asyncio.sleep(wait_seconds)
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            stored = alerts.rows()
            await alerts.close()
            await coordinator.close()

    print("\n--- what happened ---")
    for line in llm.turns:
        print(f"  transcript : {line}")
    for line in tts.spoken:
        print(f"  ATHENA said: {line}")
    fired = [line for line in tts.spoken if line.casefold().startswith("alarm:")]
    print(f"  alarms still pending: {len(stored)}")
    if fired:
        print(f"\nPASS — the alarm fired: {fired[0]}")
        return 0
    print("\nFAIL — no alarm was announced. Check the transcript above: the wake "
          "word must come first, and the time must be parseable.")
    return 1


def scenario_vad() -> int:
    """Show exactly how the gate reacts to a spoken word, frame by frame."""
    gate = VoiceGate(minimum_rms=400, noise_multiplier=2.7, end_silence_ms=260)
    script = ([("room noise", 80)] * 6
              + [("speech", 1200)] * 3
              + [("quiet consonant", 220)]
              + [("speech", 1200)] * 8
              + [("silence", 60)] * 16)
    print(f"{'frame':>5}  {'label':<16} {'rms':>6} {'threshold':>9} {'release':>7}  state")
    activated_at = None
    ended_at = None
    for index, (label, amplitude) in enumerate(script, start=1):
        from array import array
        gate.process(array("h", [amplitude] * 320).tobytes())
        if gate.active and activated_at is None:
            activated_at = index
        if gate.should_end and ended_at is None:
            ended_at = index
        state = "LISTENING" if gate.active else ("done" if ended_at else "idle")
        print(f"{index:>5}  {label:<16} {gate.last_rms:>6.0f} "
              f"{gate.threshold:>9.0f} {gate.release_threshold:>7.0f}  {state}")
    print(f"\nopened at frame {activated_at}, closed at frame {ended_at}")
    print(f"enough speech to accept a transcript: {gate.has_enough_speech}")
    print("\nA dip above 'release' keeps the turn open; a frame has to fall below "
          "'release' to count as silence.")
    return 0


async def scenario_chat() -> int:
    """Type to ATHENA. Alarms really fire while you keep talking."""
    with tempfile.TemporaryDirectory(prefix="athena-harness-") as directory:
        root = Path(directory)
        sandbox_environment(root)
        settings = build_settings(root)
        registry, alerts = build_registry(settings)
        llm = OfflineLanguageModel(registry)
        alerts.notify = lambda text: print(f"\n*** {text}\n\nYou: ", end="") or True
        await alerts.start()
        print("ATHENA offline harness. No API keys are used.")
        print("Try: set an alarm for 10 seconds to test  |  list alarms  |  /quit")
        try:
            while True:
                text = (await asyncio.to_thread(input, "You: ")).strip()
                if not text:
                    continue
                if text in {"/quit", "/exit", "quit", "exit"}:
                    break
                async for fragment in llm.stream_reply(uuid4(), text):
                    print(f"ATHENA: {fragment}")
        finally:
            await alerts.close()
    return 0


def scenario_stt(seconds: float, utterances: int) -> int:
    """Compare speech-recognition models on what they actually cost."""
    from athena.config import ASR_MODELS

    print("Speech recognition is billed per second of INPUT AUDIO.")
    print("The transcript itself is not charged, so there is no STT token cost:")
    print("the lever is how many seconds of audio reach the service, and which model.")
    print()
    print(f"Assumed: {seconds:g}s of audio per utterance, {utterances} utterances per day.")
    print()
    header = f"{'model':<38}{'CNY/sec':>9}{'CNY/day':>10}{'CNY/month':>11}  note"
    print(header)
    print("-" * len(header))
    baseline = None
    for name, (price, note) in ASR_MODELS.items():
        daily = price * seconds * utterances
        baseline = daily if baseline is None else baseline
        print(f"{name:<38}{price:>9.5f}{daily:>10.3f}{daily * 30:>11.2f}  {note}")
    print()
    print("ATHENA only sends audio while its own voice gate is open, so silence costs")
    print("nothing. What is sent per utterance is the speech plus the pre-roll buffer.")
    print()
    print("Latency, in the order it is paid:")
    print("  1. the voice gate confirming speech onset      (~100 ms, local)")
    print("  2. the DashScope websocket handshake           (removed by pre-warming)")
    print("  3. the service finalising the last sentence     (max_sentence_silence)")
    print()
    print("Step 2 used to happen after the user had already started talking. A session")
    print("is now kept warm between turns, so it is paid during the silence instead.")
    print("Set ATHENA_STT_PREWARM=0 to compare, and watch 'Speech recognition pre-warm'")
    print("in the voice log. Set ATHENA_STT_MAX_SENTENCE_SILENCE_MS to match the local")
    print("gate (vad_end_silence_ms) so the transcript is not finalised twice.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", nargs="?", default="alarms",
                        choices=["alarms", "vad", "chat", "stt"])
    parser.add_argument("--utterance", default="athena set an alarm for 3 seconds",
                        help="what the fake microphone hears")
    parser.add_argument("--wait", type=float, default=12.0,
                        help="seconds to let ATHENA run")
    parser.add_argument("--seconds", type=float, default=3.0,
                        help="audio seconds per utterance, for the stt scenario")
    parser.add_argument("--utterances", type=int, default=100,
                        help="utterances per day, for the stt scenario")
    args = parser.parse_args()

    if args.scenario == "vad":
        return scenario_vad()
    if args.scenario == "chat":
        return asyncio.run(scenario_chat())
    if args.scenario == "stt":
        return scenario_stt(args.seconds, args.utterances)
    return asyncio.run(scenario_alarms(args.utterance, args.wait))


if __name__ == "__main__":
    raise SystemExit(main())
