# A.T.H.E.N.A.

A.T.H.E.N.A. is an open-source, JARVIS-inspired personal voice agent built in Python.
It runs continuously on a small Linux computer such as an Orange Pi while using cloud
models for conversation, speech recognition, and speech generation.

## Current features

- Wake-word voice interaction with a short follow-up listening window
- Streaming Qwen speech recognition and text-to-speech
- DeepSeek conversation and tool calling
- Shared memory across voice, terminal, web, and Feishu interfaces
- Local-network control dashboard
- Feishu remote messaging
- Weather, web browsing, downloads with approval, and sandboxed coding tools
- NetEase Cloud Music playback with pause, resume, skip, stop, and volume control
- Signed local-network updates from a development computer to an Orange Pi

## Quick start

Requirements: Python 3.11 or newer, PortAudio, and FFmpeg.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
```

Fill in `.env` with your own DeepSeek and Alibaba Cloud Model Studio credentials, then
start a supported interface:

```powershell
athena-chat
```

The complete Orange Pi installation and update instructions are in
[`orange_pi/README.md`](orange_pi/README.md).

## Security

- Never commit `.env`, `API KEY.txt`, update signing keys, databases, or downloaded files.
- Downloads and local command execution require explicit user approval.
- The dashboard accepts local-network connections only and requires authentication.
- Website text and tool output are treated as untrusted data.

If a credential has ever been published, rotate it immediately; deleting it from the
latest commit is not enough because Git retains history.

## Tests

```powershell
python -m unittest discover -s tests
```

## License

MIT. See [`LICENSE`](LICENSE).
