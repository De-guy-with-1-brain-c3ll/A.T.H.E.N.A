"""Authenticated local-network control panel for ATHENA."""
from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict, defaultdict, deque
import hashlib
import hmac
import io
import ipaddress
import os
from pathlib import Path
import secrets
import time
import wave
from uuid import UUID

from aiohttp import web

from athena.background import BackgroundAgents
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.memory.database import MemoryDatabase
from athena.memory.service import MemoryService
from athena.paths import database_path
from athena.prompts import read_prompt, write_prompt
from athena.settings.store import RuntimeSettingsStore
from athena.text import TextSession
from athena.tools.registry import ToolRegistry
from athena.tts.qwen import QwenRealtimeSynthesizer
from athena.voice_ipc import request_music, request_speech


COOKIE = "athena_session"
SESSION_SECONDS = 24 * 60 * 60
STATIC = Path(__file__).resolve().parent / "web_static"


class DashboardState:
    def __init__(self) -> None:
        load_local_environment()
        self.password = os.environ.get("ATHENA_WEB_PASSWORD", "").strip()
        self.secret = os.environ.get("ATHENA_WEB_SECRET", "").encode()
        if len(self.password) < 10:
            raise ValueError("ATHENA_WEB_PASSWORD must contain at least 10 characters.")
        if len(self.secret) < 32:
            raise ValueError("ATHENA_WEB_SECRET must contain at least 32 characters.")
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise ValueError("DEEPSEEK_API_KEY is missing.")
        self.settings = RuntimeSettingsStore()
        self.dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        self.tts_model = os.environ.get("ATHENA_TTS_MODEL", "qwen3-tts-flash-realtime").strip()
        self.speech_lock = asyncio.Lock()
        registry = ToolRegistry.discover(services={"settings": self.settings})
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

    async def start(self) -> None:
        await self.memory.connect()

    async def close(self) -> None:
        await self.background.cancel_all()
        await asyncio.gather(self.session.model.close(), self.memory.close(),
                             return_exceptions=True)

    def issue_session(self) -> str:
        payload = f"{int(time.time())}.{secrets.token_urlsafe(18)}"
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def valid_session(self, token: str) -> bool:
        try:
            stamp, nonce, signature = token.split(".", 2)
            payload = f"{stamp}.{nonce}"
            expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
            age = time.time() - int(stamp)
            return -60 <= age <= SESSION_SECONDS and hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    def csrf(self, token: str) -> str:
        return hmac.new(self.secret, ("csrf:" + token).encode(), hashlib.sha256).hexdigest()

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


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def login(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    if not state.may_try_login(request.remote or "unknown"):
        raise web.HTTPTooManyRequests(text="Too many attempts. Wait one minute.")
    body = await _json_body(request)
    if not hmac.compare_digest(str(body.get("password", "")), state.password):
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
    app.router.add_post("/api/prompts", save_prompts)
    app.router.add_get("/api/conversations", conversations)
    app.router.add_post("/api/chat", submit_chat)
    app.router.add_get("/api/chat/{identity}", chat_status)
    app.router.add_post("/api/speech/athena", speak_on_athena)
    app.router.add_post("/api/speech/device", speak_on_device)
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
