from __future__ import annotations

import asyncio
import sys

from athena.audio.capture import Microphone
from athena.audio.playback import Speaker
from athena.audio.vad import VoiceGate
from athena.config import Settings, load_local_environment
from athena.coordinator import VoiceCoordinator
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.memory.service import MemoryService
from athena.stt.fun_asr import FunAsrRecognizer
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry
from athena.tts.qwen import QwenRealtimeSynthesizer
from athena.voice_ipc import VoiceControlServer
from athena.tools.netease import NetEasePlayer


async def run() -> None:
    load_local_environment()
    settings_store = RuntimeSettingsStore()
    settings = Settings.from_environment(settings_store)
    speaker = Speaker(settings.tts_sample_rate, device=settings.audio_output_device)
    netease_player = NetEasePlayer(speaker)
    tools = ToolRegistry.discover(services={"settings": settings_store, "netease_player": netease_player})
    coordinator = VoiceCoordinator(
        microphone=Microphone(settings.stt_sample_rate, device=settings.audio_input_device),
        speaker=speaker,
        stt=FunAsrRecognizer(
            settings.dashscope_api_key,
            settings.stt_model,
            settings.stt_sample_rate,
            settings.stt_language,
        ),
        llm=DeepSeekLanguageModel(
            settings.deepseek_api_key,
            settings.deepseek_model,
            tools,
            settings_store,
        ),
        tts=QwenRealtimeSynthesizer(
            settings.dashscope_api_key,
            settings.tts_model,
            settings.tts_voice,
            settings=settings_store,
        ),
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
    control = VoiceControlServer(coordinator)
    try:
        await coordinator.connect()
        await control.start()
        await coordinator.run()
    finally:
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
