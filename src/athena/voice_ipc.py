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
