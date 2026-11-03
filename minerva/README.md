# Minerva — OpenClaw-based voice agent framework

## Structure
- `minerva/core.py` — turn coordinator: STT → OpenClaw → TTS
- `minerva/stt/` — local speech recognition (SenseVoice via sherpa-onnx, adapted from athena)
- `minerva/tts/` — local text-to-speech (Piper, adapted from athena)
- `minerva/events.py` — shared Transcript / AudioChunk dataclasses
- `tests/` — test suite (to be added)

## Getting started
```powershell
cd minerva
.\.venv\Scripts\python.exe -m minerva.main
```

## Design notes
- STT and TTS run locally (no API key, no per-character cost)
- OpenClaw handles the model + tools + memory layer
- One turn active at a time; interruption supported
