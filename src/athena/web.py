"""Authenticated local-network control panel for ATHENA."""
from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict, defaultdict, deque
import hashlib
import hmac
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import time
import wave
from uuid import UUID

from aiohttp import WSMsgType, web

from athena.background import BackgroundAgents
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.memory.database import MemoryDatabase
from athena.memory.service import MemoryService
from athena.paths import database_path
from athena.prompts import read_prompt, write_prompt
from athena.remote_audio import FRAME_MS, MICROPHONE_RATE, SPEAKER_RATE
from athena.services import build_registry
from athena.settings.store import RuntimeSettingsStore
from athena.text import TextSession
from athena.tts.qwen import QwenRealtimeSynthesizer
from athena.voice_ipc import (
    AUDIO_FRAME,
    FLUSH_FRAME,
    MAX_FRAME_BYTES,
    open_audio_stream,
    pack_frame,
    read_frame,
    request_music,
    request_speech,
    request_volume,
)
from athena.web_auth import COOKIE, SESSION_SECONDS, SessionAuth


STATIC = Path(__file__).resolve().parent / "web_static"


class DashboardState:
    def __init__(self) -> None:
        load_local_environment()
        self.auth = SessionAuth.from_environment()
        self.password = self.auth.password
        self.secret = self.auth.secret
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise ValueError("DEEPSEEK_API_KEY is missing.")
        self.settings = RuntimeSettingsStore()
        self.dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        self.tts_model = os.environ.get("ATHENA_TTS_MODEL", "qwen3-tts-flash-realtime").strip()
        self.speech_lock = asyncio.Lock()
        registry, alerts = build_registry(self.settings)
        self.alerts = alerts
        self.alerts.notify = self._announce_alarm
        model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
        model = DeepSeekLanguageModel(key, model_name, registry, self.settings, interface="text")
        self.memory = MemoryService(
            database_path(), key, model_name,
            self.settings.get("memory_batch_delay_seconds"), self.settings,
        )
        self.session = TextSession(model, self.memory, registry)
        self.background = BackgroundAgents(model, limit=3)
        self.completed: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.finalize_lock = asyncio.Lock()
        self.login_attempts: dict[str, deque[float]] = defaultdict(deque)

    async def _announce_alarm(self, text: str) -> bool:
        """Speak an alarm through the voice service when one is running."""
        print(f"ATHENA alarm: {text}")
        try:
            await request_speech(text)
            return True
        except Exception:
            # The dashboard is a separate process; without the voice service the
            # log line above is the only delivery, which is stated honestly.
            return False

    async def start(self) -> None:
        await self.memory.connect()
        await self.alerts.start()

    async def close(self) -> None:
        await self.alerts.close()
        await self.background.cancel_all()
        await asyncio.gather(self.session.model.close(), self.memory.close(),
                             return_exceptions=True)

    def issue_session(self) -> str:
        return self.auth.issue()

    def valid_session(self, token: str) -> bool:
        return self.auth.valid(token)

    def csrf(self, token: str) -> str:
        return self.auth.csrf(token)

    def may_try_login(self, address: str) -> bool:
        now = time.monotonic()
        attempts = self.login_attempts[address]
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= 8:
            return False
        attempts.append(now)
        return True


def _is_local(remote: str | None) -> bool:
    try:
        address = ipaddress.ip_address((remote or "").split("%", 1)[0])
        return address.is_private or address.is_loopback or address.is_link_local
    except ValueError:
        return False


@web.middleware
async def local_network_only(request: web.Request, handler):
    if not _is_local(request.remote):
        raise web.HTTPForbidden(text="ATHENA's dashboard is local-network only.")
    return await handler(request)


def _authenticated(request: web.Request) -> bool:
    state: DashboardState = request.app["state"]
    # A switched-off password means every request is already trusted. The CSRF
    # check in _require_post still applies, so this is not the same as having no
    # defences: a page on another site cannot read the token and so cannot drive
    # the dashboard, which is the case that matters for a browser on this LAN.
    if state.auth.disabled:
        return True
    return state.valid_session(request.cookies.get(COOKIE, ""))


def _require_auth(request: web.Request) -> DashboardState:
    if not _authenticated(request):
        raise web.HTTPUnauthorized(text="Sign in to ATHENA.")
    return request.app["state"]


def _require_post(request: web.Request) -> DashboardState:
    state = _require_auth(request)
    token = request.cookies.get(COOKIE, "")
    supplied = request.headers.get("X-ATHENA-CSRF", "")
    if not hmac.compare_digest(supplied, state.csrf(token)):
        raise web.HTTPForbidden(text="The dashboard security token is invalid. Refresh the page.")
    return state


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Send a valid JSON request.") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Send a JSON object.")
    return body


async def index(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    response = web.FileResponse(STATIC / "index.html")
    # With the password switched off there is no login step to hand out a
    # session, so the page issues one itself. The session is what the CSRF token
    # is derived from, so skipping this would leave every write rejected with
    # "refresh the page" — a dashboard that loads but cannot do anything.
    if state.auth.disabled and not state.valid_session(request.cookies.get(COOKIE, "")):
        token = state.issue_session()
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            samesite="Strict", path="/")
    return response


async def login(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    if not state.may_try_login(request.remote or "unknown"):
        raise web.HTTPTooManyRequests(text="Too many attempts. Wait one minute.")
    body = await _json_body(request)
    if not state.auth.accepts_password(str(body.get("password", ""))):
        await asyncio.sleep(0.35)
        raise web.HTTPUnauthorized(text="Wrong dashboard password.")
    token = state.issue_session()
    response = web.json_response({"ok": True, "csrf": state.csrf(token)})
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        samesite="Strict", path="/")
    return response


async def logout(request: web.Request) -> web.Response:
    _require_post(request)
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE, path="/")
    return response


async def _service_action(action: str | None = None) -> tuple[str, str]:
    if action:
        process = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/usr/bin/systemctl", action, "athena-voice.service",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(process.communicate(), 20)
        if process.returncode:
            raise RuntimeError(error.decode(errors="replace").strip() or "Service control failed.")
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/systemctl", "is-active", "athena-voice.service",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    status = output.decode().strip() or "unknown"
    return status, "ATHENA voice is running." if status == "active" else "ATHENA voice is stopped."


async def bootstrap(request: web.Request) -> web.Response:
    state = _require_auth(request)
    status, label = await _service_action()
    token = request.cookies.get(COOKIE, "")
    return web.json_response({
        "csrf": state.csrf(token), "service": status, "serviceLabel": label,
        "prompts": {"system": read_prompt("system"), "memory": read_prompt("memory")},
    })


async def service_status(request: web.Request) -> web.Response:
    _require_auth(request)
    status, label = await _service_action()
    return web.json_response({"service": status, "serviceLabel": label})


async def service_control(request: web.Request) -> web.Response:
    _require_post(request)
    body = await _json_body(request)
    action = str(body.get("action", ""))
    if action not in {"start", "stop", "restart"}:
        raise web.HTTPBadRequest(text="Unknown service action.")
    try:
        status, label = await _service_action(action)
    except (RuntimeError, asyncio.TimeoutError) as error:
        raise web.HTTPInternalServerError(text=str(error)) from None
    return web.json_response({"service": status, "serviceLabel": label})


async def music_status(request: web.Request) -> web.Response:
    _require_auth(request)
    try:
        return web.json_response(await request_music("status"))
    except RuntimeError as error:
        raise web.HTTPServiceUnavailable(text=str(error)) from None


async def music_control(request: web.Request) -> web.Response:
    _require_post(request)
    body = await _json_body(request)
    action = str(body.get("action", ""))
    if action not in {"toggle", "pause", "resume", "next", "stop", "volume"}:
        raise web.HTTPBadRequest(text="Unknown music action.")
    value = body.get("value")
    try:
        value = int(value) if value is not None else None
        return web.json_response(await request_music(action, value))
    except (RuntimeError, ValueError) as error:
        raise web.HTTPBadRequest(text=str(error)) from None


async def volume_status(request: web.Request) -> web.Response:
    """The speaker level, so the slider opens where it was left."""
    _require_auth(request)
    try:
        return web.json_response(await request_volume())
    except RuntimeError as error:
        raise web.HTTPServiceUnavailable(text=str(error)) from None


async def volume_control(request: web.Request) -> web.Response:
    _require_post(request)
    body = await _json_body(request)
    value = body.get("value")
    if value is None:
        raise web.HTTPBadRequest(text="Volume must be between 0 and 100.")
    try:
        return web.json_response(await request_volume(int(value)))
    except (RuntimeError, ValueError) as error:
        raise web.HTTPBadRequest(text=str(error)) from None


async def settings_status(request: web.Request) -> web.Response:
    """Every tunable setting, so the dashboard can render editors for them."""
    state = _require_auth(request)
    return web.json_response({"settings": state.settings.public_settings()})


async def settings_control(request: web.Request) -> web.Response:
    """Change one setting, or reset it to its default with action="reset"."""
    state = _require_post(request)
    body = await _json_body(request)
    name = str(body.get("name", ""))
    if not name:
        raise web.HTTPBadRequest(text="A setting name is required.")
    try:
        if body.get("action") == "reset":
            value, live = state.settings.reset(name)
        else:
            if "value" not in body:
                raise web.HTTPBadRequest(text="A value is required.")
            value, live = state.settings.set(name, body["value"])
    except (ValueError, TypeError, OSError) as error:
        raise web.HTTPBadRequest(text=str(error)) from None
    return web.json_response({
        "ok": True,
        "name": name,
        "value": value,
        "applies_live": live,
    })


async def save_prompts(request: web.Request) -> web.Response:
    _require_post(request)
    body = await _json_body(request)
    try:
        write_prompt("system", str(body.get("system", "")))
        write_prompt("memory", str(body.get("memory", "")))
    except (OSError, ValueError) as error:
        raise web.HTTPBadRequest(text=str(error)) from None
    return web.json_response({
        "ok": True,
        "message": "Prompts saved permanently. Restart ATHENA to apply them to voice; web chat applies them after the dashboard next restarts.",
    })


async def conversations(request: web.Request) -> web.Response:
    _require_auth(request)
    database = MemoryDatabase(database_path())
    await asyncio.to_thread(database.initialize)
    rows = await asyncio.to_thread(database.recent_conversations, 40)
    return web.json_response({"conversations": [
        {"id": str(row.turn_id), "at": row.started_at,
         "user": row.user_text, "assistant": row.assistant_text}
        for row in rows
    ]})


async def submit_chat(request: web.Request) -> web.Response:
    state = _require_post(request)
    body = await _json_body(request)
    text = str(body.get("text", "")).strip()
    if not text or len(text) > 4000:
        raise web.HTTPBadRequest(text="Enter a message of 1 to 4,000 characters.")
    job = state.background.submit(text, state.memory.context_messages())
    if job is None:
        raise web.HTTPTooManyRequests(text="Three ATHENA tasks are already running.")
    return web.json_response({"id": str(job.id), "status": "running"}, status=202)


async def chat_status(request: web.Request) -> web.Response:
    state = _require_auth(request)
    identity = request.match_info["identity"]
    cached = state.completed.get(identity)
    if cached:
        return web.json_response({"id": identity, "status": "complete", "answer": cached[1]})
    try:
        job = state.background.jobs.get(UUID(identity))
    except ValueError:
        job = None
    if job is None:
        raise web.HTTPNotFound(text="That chat task no longer exists.")
    if job.reply is None:
        return web.json_response({"id": identity, "status": "running"})
    async with state.finalize_lock:
        cached = state.completed.get(identity)
        if cached:
            answer = cached[1]
        else:
            answer = (job.reply or "ATHENA returned no text.").strip()
            await state.session.remember_job(job)
            state.background.delivered(job)
            state.completed[identity] = (time.monotonic(), answer)
            while len(state.completed) > 50:
                state.completed.popitem(last=False)
    return web.json_response({"id": identity, "status": "complete", "answer": answer})


def _spoken_text(text: str) -> str:
    """Remove common Markdown noise before sending a dashboard answer to TTS."""
    import re
    text = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"```.*?```", " Code omitted. ", text, flags=re.DOTALL)
    text = re.sub(r"[`*_#>]", "", text)
    return " ".join(text.split())[:3000]


async def speak_on_athena(request: web.Request) -> web.Response:
    _require_post(request)
    body = await _json_body(request)
    text = _spoken_text(str(body.get("text", "")))
    if not text:
        raise web.HTTPBadRequest(text="There is no reply to speak.")
    try:
        await request_speech(text)
    except RuntimeError as error:
        raise web.HTTPConflict(text=str(error)) from None
    return web.json_response({"ok": True, "message": "Playing on the ATHENA speaker."})


async def speak_on_device(request: web.Request) -> web.Response:
    state = _require_post(request)
    body = await _json_body(request)
    text = _spoken_text(str(body.get("text", "")))
    if not text:
        raise web.HTTPBadRequest(text="There is no reply to speak.")
    if not state.dashscope_key:
        raise web.HTTPServiceUnavailable(text="DASHSCOPE_API_KEY is missing on the Orange Pi.")
    synthesizer = QwenRealtimeSynthesizer(
        state.dashscope_key, state.tts_model, state.settings.get("tts_voice"),
        settings=state.settings,
    )
    try:
        async with state.speech_lock:
            pcm = await asyncio.wait_for(synthesizer.cache_phrase(text), 40)
    except (Exception, asyncio.TimeoutError):
        raise web.HTTPBadGateway(text="Qwen could not generate speech for this reply.") from None
    finally:
        await synthesizer.close()
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(pcm)
    return web.Response(body=output.getvalue(), content_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


async def audio_stream(request: web.Request) -> web.WebSocketResponse:
    """Relay this browser's microphone and speaker to the voice process.

    The dashboard already refuses non-local addresses and requires a session, so
    sharing a device's audio needs no extra port, no tunnel and no second login.
    """
    _require_auth(request)
    socket = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=30)
    await socket.prepare(request)
    try:
        reader, writer = await open_audio_stream()
    except RuntimeError as error:
        await socket.send_json({"type": "error", "message": str(error)})
        await socket.close()
        return socket

    await socket.send_json({"type": "ready", "microphone_rate": MICROPHONE_RATE,
                            "speaker_rate": SPEAKER_RATE, "frame_ms": FRAME_MS})

    async def from_browser() -> None:
        async for message in socket:
            if message.type == WSMsgType.BINARY:
                writer.write(pack_frame(AUDIO_FRAME, message.data))
                await writer.drain()
            elif message.type == WSMsgType.TEXT:
                try:
                    control = json.loads(message.data)
                except ValueError:
                    continue
                if isinstance(control, dict) and control.get("type") == "flush":
                    writer.write(pack_frame(FLUSH_FRAME))
                    await writer.drain()

    async def to_browser() -> None:
        while True:
            frame_type, payload = await read_frame(reader)
            if frame_type == AUDIO_FRAME:
                await socket.send_bytes(payload)
            elif frame_type == FLUSH_FRAME:
                await socket.send_json({"type": "flush"})

    tasks = [asyncio.create_task(from_browser()), asyncio.create_task(to_browser())]
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except (ConnectionResetError, RuntimeError, ValueError, OSError):
        pass
    finally:
        # Retrieve every task, including the one that finished with the error
        # that ended the session: awaiting only the pending ones leaves that
        # exception unretrieved and prints a traceback at shutdown.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        await socket.close()
    return socket


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def create_app() -> web.Application:
    state = DashboardState()
    await state.start()
    app = web.Application(middlewares=[local_network_only], client_max_size=64 * 1024)
    app["state"] = state
    app.router.add_get("/", index)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_get("/api/status", service_status)
    app.router.add_post("/api/service", service_control)
    app.router.add_get("/api/music", music_status)
    app.router.add_post("/api/music", music_control)
    app.router.add_get("/api/volume", volume_status)
    app.router.add_post("/api/volume", volume_control)
    app.router.add_post("/api/prompts", save_prompts)
    app.router.add_get("/api/settings", settings_status)
    app.router.add_post("/api/settings", settings_control)
    app.router.add_get("/api/conversations", conversations)
    app.router.add_post("/api/chat", submit_chat)
    app.router.add_get("/api/chat/{identity}", chat_status)
    app.router.add_post("/api/speech/athena", speak_on_athena)
    app.router.add_post("/api/speech/device", speak_on_device)
    app.router.add_get("/ws/audio", audio_stream)
    app.router.add_get("/health", health)
    app.router.add_static("/static", STATIC, show_index=False)

    async def cleanup(_: web.Application) -> None:
        await state.close()
    app.on_cleanup.append(cleanup)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("ATHENA_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ATHENA_WEB_PORT", "8780")))
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
