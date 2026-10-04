"""Small local-only control socket between the dashboard and voice service."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


SOCKET_PATH = Path(os.environ.get("ATHENA_VOICE_SOCKET", "/run/athena/voice-control.sock"))

# Every music command the dashboard may drive: transport, the volume, and the
# saved playlists. Kept here rather than in the web layer because this socket is
# the one the voice process actually trusts.
MUSIC_COMMANDS = frozenset({
    "status", "toggle", "pause", "resume", "next", "stop", "volume",
    "add_playlist", "remove_playlist", "play_playlist", "auto_play",
})


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
                if command not in MUSIC_COMMANDS:
                    raise ValueError("Invalid music-control request.")
                value = request.get("value")
                value = int(value) if value is not None else None
                name = request.get("name")
                moods = request.get("moods")
                if moods is not None and not isinstance(moods, list):
                    raise ValueError("Invalid music-control request.")
                response = {"ok": True, **await self.coordinator.control_music(
                    command, value,
                    name=str(name) if name is not None else None,
                    query=str(request.get("query", "")),
                    moods=[str(mood) for mood in moods] if moods else None)}
            elif action == "volume":
                value = request.get("value")
                if value is None:
                    response = {"ok": True, "volume": self.coordinator.volume}
                else:
                    response = {"ok": True,
                                "volume": await self.coordinator.set_volume(int(value))}
            elif action == "audio_route":
                response = {"ok": True, **await self.coordinator.request_audio_route(request.get("target", "status"))}
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

async def request_audio_route(target="status", path: Path = SOCKET_PATH) -> dict:
    return await _request({"action": "audio_route", "target": target}, path)


async def request_music(command: str, value: int | None = None, *,
                        name: str | None = None, query: str = "",
                        moods: list[str] | None = None,
                        path: Path = SOCKET_PATH) -> dict:
    payload: dict[str, object] = {"action": "music", "command": command}
    if value is not None:
        payload["value"] = value
    if name is not None:
        payload["name"] = name
    if query:
        payload["query"] = query
    if moods:
        payload["moods"] = list(moods)
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
        raw = await asyncio.wait_for(reader.readline(), 12 if payload.get("action") == "audio_route" else 3)
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
# Anything else the two sides need to say to each other — what the audio format
# is about to be, and what the browser can do for itself — as JSON.
CONTROL_FRAME = 3
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
        else:
            self._writer.write(pack_frame(CONTROL_FRAME,
                                          json.dumps(payload).encode("utf-8")))
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
            if hasattr(self.audio, "selected_route"):
                await self.audio.send_json({"type": "audio_route", "target": self.audio.selected_route})
            while True:
                frame_type, payload = await read_frame(reader)
                if frame_type == AUDIO_FRAME:
                    self.audio.push(payload)
                elif frame_type == FLUSH_FRAME:
                    self.audio.drain()
                elif frame_type == CONTROL_FRAME:
                    try:
                        self.audio.control(json.loads(payload or b"{}"))
                    except ValueError:
                        continue
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
