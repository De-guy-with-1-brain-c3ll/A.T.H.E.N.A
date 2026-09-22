"""Small local-only control socket between the dashboard and voice service."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


SOCKET_PATH = Path(os.environ.get("ATHENA_VOICE_SOCKET", "/run/athena/voice-control.sock"))


class VoiceControlServer:
    def __init__(self, coordinator, path: Path = SOCKET_PATH) -> None:
        self.coordinator = coordinator
        self.path = path
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._handle, path=self.path)
        self.path.chmod(0o660)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        response: dict[str, object]
        try:
            raw = await asyncio.wait_for(reader.readline(), 2)
            if len(raw) > 8192 or not raw.endswith(b"\n"):
                raise ValueError("Invalid voice-control request.")
            request = json.loads(raw)
            action = request.get("action")
            if action == "speak":
                text = str(request.get("text", "")).strip()
                if not text or len(text) > 4000:
                    raise ValueError("Invalid voice-control request.")
                accepted = self.coordinator.enqueue_external_speech(text)
                response = {"ok": accepted}
                if not accepted:
                    response["error"] = "The ATHENA speaker queue is full."
            elif action == "music":
                command = str(request.get("command", "")).strip()
                if command not in {"status", "toggle", "pause", "resume", "next", "stop", "volume"}:
                    raise ValueError("Invalid music-control request.")
                value = request.get("value")
                value = int(value) if value is not None else None
                response = {"ok": True, **await self.coordinator.control_music(command, value)}
            elif action == "volume":
                value = request.get("value")
                if value is None:
                    response = {"ok": True, "volume": self.coordinator.volume}
                else:
                    response = {"ok": True,
                                "volume": await self.coordinator.set_volume(int(value))}
            else:
                raise ValueError("Invalid voice-control request.")
        except (ValueError, TypeError, RuntimeError, OSError, json.JSONDecodeError, asyncio.TimeoutError) as error:
            response = {"ok": False, "error": str(error)}
        writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        self.path.unlink(missing_ok=True)


async def request_speech(text: str, path: Path = SOCKET_PATH) -> None:
    await _request({"action": "speak", "text": text}, path)


async def request_music(command: str, value: int | None = None,
                        path: Path = SOCKET_PATH) -> dict:
    payload: dict[str, object] = {"action": "music", "command": command}
    if value is not None:
        payload["value"] = value
    return await _request(payload, path)


async def request_volume(value: int | None = None,
                         path: Path = SOCKET_PATH) -> dict:
    """Read the speaker level, or set it when a percentage is given."""
    payload: dict[str, object] = {"action": "volume"}
    if value is not None:
        payload["value"] = value
    return await _request(payload, path)


async def _request(payload: dict, path: Path) -> dict:
    reader = writer = None
    for attempt in range(5):
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), 2)
            break
        except (OSError, asyncio.TimeoutError):
            if attempt == 4:
                raise RuntimeError("ATHENA voice is offline. Start it before using its speaker.") from None
            await asyncio.sleep(0.25)
    try:
        encoded = json.dumps(payload, separators=(",", ":"))
        writer.write(encoded.encode() + b"\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), 3)
        response = json.loads(raw)
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "ATHENA rejected the speech request."))
        return response
    finally:
        writer.close()
        await writer.wait_closed()


# --------------------------------------------------------------------------
# Browser audio bridge.
#
# The microphone and speaker live in the voice process, but the page that uses
# them is served by the dashboard, which is a different process. They talk over
# this local-only Unix socket, so the browser never needs its own port and the
# dashboard's session and local-network-only rule keep protecting it.
# --------------------------------------------------------------------------

AUDIO_SOCKET_PATH = Path(os.environ.get(
    "ATHENA_VOICE_AUDIO_SOCKET", "/run/athena/voice-audio.sock"))
AUDIO_FRAME = 1
FLUSH_FRAME = 2
MAX_FRAME_BYTES = 1 << 20


def pack_frame(frame_type: int, payload: bytes = b"") -> bytes:
    return bytes([frame_type]) + len(payload).to_bytes(4, "big") + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(5)
    length = int.from_bytes(header[1:], "big")
    if length > MAX_FRAME_BYTES:
        raise ValueError("Audio frame is too large.")
    payload = await reader.readexactly(length) if length else b""
    return header[0], payload


class SocketSink:
    """Presents the audio sink the voice process expects over a stream socket."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer

    @property
    def closed(self) -> bool:
        return self._writer.is_closing()

    async def send_bytes(self, pcm: bytes) -> None:
        self._writer.write(pack_frame(AUDIO_FRAME, pcm))
        await self._writer.drain()

    async def send_json(self, payload: dict) -> None:
        if payload.get("type") == "flush":
            self._writer.write(pack_frame(FLUSH_FRAME))
            await self._writer.drain()


VOICE_OFFLINE = "ATHENA voice is offline. Start it before sharing this device's audio."


def _unix_streams_supported() -> bool:
    return hasattr(asyncio, "open_unix_connection") and hasattr(asyncio, "start_unix_server")


class VoiceAudioServer:
    """Accept one browser audio connection relayed by the dashboard."""

    def __init__(self, audio, path: Path | None = None) -> None:
        self.audio = audio
        # Resolved when used, so the socket location can be redirected.
        self.path = Path(path) if path is not None else AUDIO_SOCKET_PATH
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if not _unix_streams_supported():
            raise RuntimeError("Browser audio needs a POSIX host for its local socket.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._handle, path=self.path)
        self.path.chmod(0o660)

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        sink = SocketSink(writer)
        # A new browser takes over from any previous one.
        self.audio.attach(sink)
        self.audio.drain()
        try:
            while True:
                frame_type, payload = await read_frame(reader)
                if frame_type == AUDIO_FRAME:
                    self.audio.push(payload)
                elif frame_type == FLUSH_FRAME:
                    self.audio.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError, OSError):
            pass
        finally:
            self.audio.detach(sink)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.path.unlink(missing_ok=True)


async def open_audio_stream(path: Path | None = None):
    """Connect the dashboard to the voice process's audio bridge."""
    if not _unix_streams_supported():
        raise RuntimeError(VOICE_OFFLINE)
    target = Path(path) if path is not None else AUDIO_SOCKET_PATH
    try:
        return await asyncio.wait_for(asyncio.open_unix_connection(target), 5)
    except (OSError, asyncio.TimeoutError):
        raise RuntimeError(VOICE_OFFLINE) from None
