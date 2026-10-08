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
import ssl
import time
import wave
from uuid import UUID

from aiohttp import WSMsgType, web

from athena.audio.telemetry import DEFAULT_STATUS_PATH, read_status
from athena.background import BackgroundAgents
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.memory.database import MemoryDatabase
from athena.memory.service import MemoryService
from athena.paths import database_path
from athena.metrics import snapshot, read_progress
from athena.prompts import read_prompt, write_prompt
from athena.remote_audio import FRAME_MS, MICROPHONE_RATE, SPEAKER_CHANNELS, SPEAKER_RATE
from athena.services import build_registry
from athena.settings.store import RuntimeSettingsStore
from athena.text import TextSession
from athena.tts.qwen import QwenRealtimeSynthesizer
from athena.voice_ipc import (
    AUDIO_FRAME,
    CONTROL_FRAME,
    FLUSH_FRAME,
    MAX_FRAME_BYTES,
    MUSIC_COMMANDS,
    open_audio_stream,
    pack_frame,
    read_frame,
    request_music,
    request_speech,
    request_volume,
    request_audio_route,
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


TLS_CERT_ENV = "ATHENA_WEB_TLS_CERT"
TLS_KEY_ENV = "ATHENA_WEB_TLS_KEY"


def tls_context(cert: str = "", key: str = "") -> ssl.SSLContext | None:
    """Build the dashboard's TLS context, or ``None`` to serve plain HTTP.

    Browsers only expose the microphone in a "secure context", and a plain
    ``http://`` address on the local network is not one — so a device on the LAN
    cannot hand ATHENA its microphone, and music fails with it because the
    speaker attaches over the same socket. Serving the dashboard over TLS is what
    makes browser audio work from another machine; localhost would need no
    certificate at all, which is why this only bites on a LAN address.

    The two paths are required together. Half a key pair is always a mistake, and
    quietly falling back to HTTP would put the microphone back behind a "secure
    context" error that says nothing about the cause.
    """
    if not cert and not key:
        return None
    if not cert or not key:
        raise ValueError(f"{TLS_CERT_ENV} and {TLS_KEY_ENV} must be set together.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Below 1.2 the browsers this is used from have already refused the handshake.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


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


WINDOWS_HOST = os.name == 'nt'
VOICE_SERVICE = "athena-voice.service"
WEB_SERVICE = "athena-web.service"
SERVICE_TIMEOUT = 20
WEB_RESTART_DELAY = 1.5


async def _systemctl(*arguments: str, timeout: float = SERVICE_TIMEOUT) -> str:
    """Run one privileged systemctl argument list, returning nothing useful."""
    process = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/bin/systemctl", *arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, error = await asyncio.wait_for(process.communicate(), timeout)
    if process.returncode:
        raise RuntimeError(error.decode(errors="replace").strip() or "Service control failed.")
    return error.decode(errors="replace")


async def _service_status(service: str = VOICE_SERVICE) -> str:
    if WINDOWS_HOST:
        from athena.local_service import status
        return await asyncio.to_thread(status, 'feishu' if 'feishu' in service else 'voice')
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/systemctl", "is-active", service,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    return output.decode().strip() or "unknown"


async def _schedule_web_restart() -> None:
    """Restart the dashboard after this response has been flushed.

    athena-web cannot survive the request that restarts it, so the restart is
    detached behind a short delay. ``--no-block`` returns immediately instead of
    waiting on the unit, which would otherwise stall this coroutine until the
    very process it belongs to is killed.
    """
    if WINDOWS_HOST:
        import subprocess,sys
        command=[sys.executable,'--worker','restart-web'] if getattr(sys,'frozen',False) else [sys.executable,'-m','athena.windows_app','--worker','restart-web']
        subprocess.Popen(command,creationflags=subprocess.CREATE_NO_WINDOW,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return
    await asyncio.sleep(WEB_RESTART_DELAY)
    await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/bin/systemctl", "restart", "--no-block", WEB_SERVICE,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )


async def _service_action(action: str | None = None, everything: bool = False) -> tuple[str, str]:
    if WINDOWS_HOST:
        from athena.local_service import control
        if action:
            await asyncio.to_thread(control,action)
            if everything:
                if os.environ.get('FEISHU_APP_ID'): await asyncio.to_thread(control,action,'feishu')
                asyncio.create_task(_schedule_web_restart())
        status = await _service_status()
        return status, 'ATHENA is listening.' if status=='active' else 'ATHENA listening is stopped.'
    if action:
        if everything:
            await _systemctl(action, VOICE_SERVICE)
            await _systemctl(action, 'athena-feishu.service')
            asyncio.create_task(_schedule_web_restart())
            return await _service_status(VOICE_SERVICE), (
                "ATHENA is restarting. The dashboard will reconnect in a few seconds."
            )
        await _systemctl(action, VOICE_SERVICE)
    status = await _service_status(VOICE_SERVICE)
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
    if action not in {"start", "stop", "restart", "restart_all"}:
        raise web.HTTPBadRequest(text="Unknown service action.")
    everything = action == "restart_all"
    try:
        status, label = await _service_action("restart" if everything else action, everything)
    except (RuntimeError, asyncio.TimeoutError) as error:
        raise web.HTTPInternalServerError(text=str(error)) from None
    return web.json_response({
        "service": status, "serviceLabel": label, "restartedAll": everything,
    })


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
    if action not in MUSIC_COMMANDS:
        raise web.HTTPBadRequest(text="Unknown music action.")
    value = body.get("value")
    name = body.get("name")
    moods = body.get("moods")
    if moods is not None and not isinstance(moods, list):
        raise web.HTTPBadRequest(text="Playlist moods must be a list of short tags.")
    try:
        value = int(value) if value is not None else None
        return web.json_response(await request_music(
            action, value,
            name=str(name) if name is not None else None,
            query=str(body.get("query", "")),
            moods=[str(mood) for mood in moods] if moods else None))
    except (RuntimeError, ValueError) as error:
        raise web.HTTPBadRequest(text=str(error)) from None


STALE_SECONDS = 2.0


def speech_status_path() -> Path:
    configured = os.environ.get("ATHENA_AUDIO_STATUS_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_STATUS_PATH


async def speech_status(request: web.Request) -> web.Response:
    """What the voice process last heard, so the page can prove it heard it."""
    _require_auth(request)
    status = read_status(speech_status_path())
    updated = float(status.get("updated", 0) or 0)
    age = round(time.time() - updated, 2) if updated else None
    payload = dict(status)
    payload["age"] = age
    payload["stale"] = age is None or age > STALE_SECONDS
    # The turn's own state goes back to waiting as soon as listening stops, so
    # the end of speech is reported as an event with an age. The page can then
    # show it for a moment without inventing a state the service is not in.
    eos_at = float(status.get("eos_at", 0) or 0)
    payload["eos_age"] = round(time.time() - eos_at, 2) if eos_at else None
    return web.json_response(payload)


async def volume_status(request: web.Request) -> web.Response:
    """The speaker level, so the slider opens where it was left."""
    _require_auth(request)
    try:
        return web.json_response(await request_volume())
    except RuntimeError as error:
        raise web.HTTPServiceUnavailable(text=str(error)) from None

async def audio_route_control(request: web.Request) -> web.Response:
    if request.method == "POST":
        _require_post(request)
        body = await _json_body(request)
        target = body.get("target")
        if target not in {"computer", "pi"}: raise web.HTTPBadRequest(text="Choose pi or computer.")
    else:
        _require_auth(request); target = "status"
    try: return web.json_response(await request_audio_route(target))
    except (RuntimeError, ValueError, OSError, TimeoutError) as error:
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


async def agent_control(request: web.Request) -> web.Response:
    state = _require_post(request) if request.method == 'POST' else _require_auth(request)
    registry = state.session.registry
    if request.method == 'GET':
        manager = registry.get('agent_task').manager
        return web.json_response({'success': True, 'agents': manager.rows()})
    arguments = await _json_body(request) if request.method == 'POST' else {'action': 'status'}
    try:
        result = await registry.execute('agent_task', arguments)
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error)) from None
    return web.json_response({'success': result.success, 'message': result.spoken_text, **result.data})


async def pc_browser_status(request: web.Request) -> web.Response:
    _require_auth(request)
    from athena.pc_bridge import browser_request
    try:
        return web.json_response(await browser_request({"action": "status"}))
    except Exception:
        return web.json_response({"available": False, "keyboard_enabled": False,
                                  "error": "PC bridge is offline or not configured."})


async def pc_keyboard_control(request: web.Request) -> web.Response:
    _require_post(request)
    body = await _json_body(request)
    if type(body.get("enabled")) is not bool:
        raise web.HTTPBadRequest(text="A true/false keyboard setting is required.")
    from athena.pc_bridge import browser_request
    try:
        return web.json_response(await browser_request({"action": "keyboard_config", "enabled": body["enabled"]}))
    except Exception:
        raise web.HTTPBadGateway(text="Could not change PC keyboard control. Check the PC bridge.") from None


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
    try:
        offset = int(request.query.get('offset', '0'))
        if not 0 <= offset <= 100000:
            raise ValueError()
    except ValueError:
        raise web.HTTPBadRequest(text='Invalid history offset.') from None
    rows = await asyncio.to_thread(database.recent_conversations, 40, offset)
    return web.json_response({"conversations": [
        {"id": str(row.turn_id), "at": row.started_at,
         "user": row.user_text, "assistant": row.assistant_text}
        for row in rows
    ]})


async def web_evidence(request: web.Request) -> web.Response:
    _require_auth(request)
    from athena.web_evidence import recent
    return web.json_response({'results': await asyncio.to_thread(recent)})


async def clear_conversation_context(request: web.Request) -> web.Response:
    state = _require_post(request)
    await state.background.cancel_all()
    # Flush queued dashboard turns before moving the shared context boundary.
    await state.memory._queue.join()
    stamp = await asyncio.to_thread(state.memory._database.clear_context)
    state.memory._sync_context()
    state.completed.clear()
    return web.json_response({'ok': True, 'cleared_at': stamp,
        'message': 'Conversation context cleared. Saved history and long-term memories are preserved. Applies to the next turn on every interface.'})


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
    from athena.tts.text import speech_text
    return speech_text(text)[:3000]


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
                            "speaker_rate": SPEAKER_RATE,
                            "speaker_channels": SPEAKER_CHANNELS,
                            "frame_ms": FRAME_MS})

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
                if not isinstance(control, dict):
                    continue
                # A flush has its own frame type because the voice process does
                # more with it than pass it on. Anything else — what the
                # browser can do for itself, for instance — is relayed as
                # control JSON.
                if control.get("type") == "flush":
                    writer.write(pack_frame(FLUSH_FRAME))
                else:
                    writer.write(pack_frame(CONTROL_FRAME,
                                            json.dumps(control).encode("utf-8")))
                await writer.drain()

    async def to_browser() -> None:
        while True:
            frame_type, payload = await read_frame(reader)
            if frame_type == AUDIO_FRAME:
                await socket.send_bytes(payload)
            elif frame_type == FLUSH_FRAME:
                await socket.send_json({"type": "flush"})
            elif frame_type == CONTROL_FRAME:
                try:
                    await socket.send_json(json.loads(payload or b"{}"))
                except ValueError:
                    continue

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
    return web.json_response({"ok": True, "app": "athena", "monitor_schema": 1})


async def monitor_status(request: web.Request) -> web.Response:
    state = _require_auth(request)
    registry = state.session.registry
    # These reads never call a language model or start speech.
    metrics = await asyncio.to_thread(snapshot)
    workflow = registry.get('background_workflow')
    return web.json_response({'operations': registry.status_store.rows(),
        'workflows': workflow.manager.rows() if workflow and workflow.manager else [],
        'agents': registry.get('agent_task').manager.rows(),
        'download': read_progress('download'), 'transfer': read_progress('transfer'),
        'metrics': metrics, 'audio': read_status(speech_status_path()), 'at': time.time()})


async def reboot_pi(request: web.Request) -> web.Response:
    _require_post(request)
    if WINDOWS_HOST:raise web.HTTPBadRequest(text='Restart your PC from the Windows Start menu.')
    body = await _json_body(request)
    if body.get('confirmation') != 'REBOOT PI':
        raise web.HTTPBadRequest(text='Explicit REBOOT PI confirmation is required.')
    await _systemctl('reboot', '--no-block')
    return web.json_response({'ok': True, 'message': 'Pi reboot requested. Reconnect after it starts.'})


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
    app.router.add_get("/api/audio", speech_status)
    app.router.add_post("/api/service", service_control)
    app.router.add_get("/api/music", music_status)
    app.router.add_post("/api/music", music_control)
    app.router.add_get("/api/volume", volume_status)
    app.router.add_post("/api/volume", volume_control)
    app.router.add_get("/api/audio-route", audio_route_control)
    app.router.add_post("/api/audio-route", audio_route_control)
    app.router.add_post("/api/prompts", save_prompts)
    app.router.add_get("/api/settings", settings_status)
    app.router.add_get('/api/monitor', monitor_status)
    app.router.add_post('/api/reboot', reboot_pi)
    app.router.add_get('/api/agents', agent_control)
    app.router.add_post('/api/agents', agent_control)
    app.router.add_get("/api/pc/browser", pc_browser_status)
    app.router.add_post("/api/pc/keyboard", pc_keyboard_control)
    app.router.add_post("/api/settings", settings_control)
    app.router.add_get("/api/conversations", conversations)
    app.router.add_post('/api/context/clear', clear_conversation_context)
    app.router.add_get('/api/web-evidence', web_evidence)
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
    parser.add_argument("--tls-cert", default=os.environ.get(TLS_CERT_ENV, ""))
    parser.add_argument("--tls-key", default=os.environ.get(TLS_KEY_ENV, ""))
    args = parser.parse_args()
    try:
        context = tls_context(args.tls_cert, args.tls_key)
    except (OSError, ssl.SSLError, ValueError) as error:
        # Start anyway and the dashboard would serve plain HTTP, which looks like
        # success while the microphone stays blocked. Refuse instead.
        print(f"Dashboard cannot start: TLS is not usable ({error})", flush=True)
        return 1
    if context is not None:
        print(f"Dashboard: HTTPS on {args.host}:{args.port}", flush=True)
    web.run_app(create_app(), host=args.host, port=args.port,
                ssl_context=context, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
