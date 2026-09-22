from __future__ import annotations

import asyncio
import os
import sys

from athena.audio.capture import Microphone
from athena.audio.playback import Speaker
from athena.audio.vad import VoiceGate
from athena.config import Settings, load_local_environment
from athena.coordinator import VoiceCoordinator
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.memory.service import MemoryService
from athena.remote_audio import BrowserMicrophone, BrowserSpeaker, RemoteAudio
from athena.services import build_registry
from athena.stt import build_recognizer
from athena.settings.store import RuntimeSettingsStore
from athena.tts import build_synthesizer, synthesizer_sample_rate
from athena.voice_ipc import VoiceAudioServer, VoiceControlServer
from athena.tools.netease import NetEasePlayer


def browser_audio_enabled() -> bool:
    return os.environ.get("ATHENA_REMOTE_AUDIO", "").strip().casefold() in {"1", "true", "yes", "on"}


async def run() -> None:
    load_local_environment()
    settings_store = RuntimeSettingsStore()
    settings = Settings.from_environment(settings_store)
    # Browser mode keeps the assistant on this machine while the microphone and
    # speaker come from a device on the local network through the dashboard. The
    # Pi's own ALSA devices stay closed, so no sound hardware is needed.
    audio = RemoteAudio() if browser_audio_enabled() else None
    if audio is not None:
        speaker = BrowserSpeaker(audio, synthesizer_sample_rate(settings))
        microphone = BrowserMicrophone(audio)
    else:
        speaker = Speaker(synthesizer_sample_rate(settings),
                          device=settings.audio_output_device)
        microphone = Microphone(settings.stt_sample_rate, device=settings.audio_input_device)
    netease_player = NetEasePlayer(speaker)
    tools, alerts = build_registry(settings_store, netease_player=netease_player)
    coordinator = VoiceCoordinator(
        microphone=microphone,
        speaker=speaker,
        stt=build_recognizer(settings),
        llm=DeepSeekLanguageModel(
            settings.deepseek_api_key,
            settings.deepseek_model,
            tools,
            settings_store,
        ),
        tts=build_synthesizer(settings, settings_store),
        memory=MemoryService(
            settings.database_path,
            settings.deepseek_api_key,
            settings.deepseek_model,
            settings.memory_batch_delay_seconds,
            settings_store,
        ),
        voice_gate=VoiceGate(
            minimum_rms=settings.vad_minimum_rms,
            noise_multiplier=settings.vad_noise_multiplier,
            end_silence_ms=settings.vad_end_silence_ms,
        ),
        settings_store=settings_store,
        audio_debug=settings.audio_debug,
    )
    alerts.notify = coordinator.enqueue_external_speech
    coordinator.alerts = alerts
    # The sleep tools would otherwise run a consolidation inline, inside a spoken
    # turn, and time out on work that was going to succeed. With the coordinator
    # published they hand the job to it and answer immediately.
    sleep_tool = tools.get("sleep_mode")
    if sleep_tool is not None:
        sleep_tool.coordinator = coordinator
    # The daily Communication Journal schedule. It builds a brief at four and
    # stays quiet; ATHENA offers it when Benjamin is next there.
    if os.environ.get("ATHENA_CJ_SCHEDULE", "1").strip().casefold() in {"1", "true", "yes", "on"}:
        alerts.ensure_watch("cj_schedule", {"at": os.environ.get("ATHENA_CJ_SCHEDULE_AT", "16:00")},
                            "the Communication Journal schedule", 86_400)
    control = VoiceControlServer(coordinator)
    audio_bridge = VoiceAudioServer(audio) if audio is not None else None
    try:
        await coordinator.connect()
        await alerts.start()
        await control.start()
        if audio_bridge is not None:
            await audio_bridge.start()
        await coordinator.run()
    finally:
        if audio_bridge is not None:
            await audio_bridge.close()
        await alerts.close()
        await control.close()
        await coordinator.close()


def main() -> int:
    try:
        asyncio.run(run())
        return 0
    except KeyboardInterrupt:
        print("\nATHENA stopped.")
        return 130
    except Exception as error:
        print(f"ATHENA failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
