"""Long-range ATHENA chat through a private Feishu bot."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from uuid import uuid4

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)

from athena.background import BackgroundAgents
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.memory.service import MemoryService
from athena.paths import data_directory, database_path
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    event_id: str
    message_id: str
    open_id: str
    chat_type: str
    message_type: str
    text: str


class FeishuAccess:
    """One-time local pairing plus a persistent sender allowlist."""

    def __init__(self, path: Path, configured: str = "") -> None:
        self.path = path
        self.allowed = {item.strip() for item in configured.split(",") if item.strip()}
        if path.is_file():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                values = stored.get("allowed_open_ids", [])
                if isinstance(values, list):
                    self.allowed.update(str(item) for item in values if str(item).strip())
            except (OSError, ValueError, TypeError):
                pass
        self.pairing_code = None if self.allowed else f"{secrets.randbelow(1_000_000):06d}"

    def authorize(self, open_id: str, text: str) -> str:
        if open_id in self.allowed:
            return "allowed"
        if self.pairing_code and text.strip() == f"/pair {self.pairing_code}":
            self.allowed.add(open_id)
            self.pairing_code = None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"allowed_open_ids": sorted(self.allowed)}), encoding="utf-8")
            temporary.replace(self.path)
            return "paired"
        return "denied"


class FeishuGateway:
    def __init__(self, model, memory, registry, send_text, access: FeishuAccess, limit=5):
        self.model = model
        self.memory = memory
        self.registry = registry
        self.send_text = send_text
        self.access = access
        self.background = BackgroundAgents(model, limit=limit)
        self.inbox: asyncio.Queue[IncomingMessage | Exception] = asyncio.Queue(maxsize=100)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.recipients = {}
        self.approval_owner = None
        self._seen_order = deque(maxlen=1024)
        self._seen = set()

    def enqueue_threadsafe(self, incoming: IncomingMessage | Exception) -> None:
        if self.loop is None:
            return
        def enqueue():
            if not self.inbox.full():
                self.inbox.put_nowait(incoming)
        try:
            self.loop.call_soon_threadsafe(enqueue)
        except RuntimeError:
            pass

    async def _send(self, open_id: str, text: str) -> bool:
        try:
            await self.send_text(open_id, text)
            return True
        except Exception:
            # A temporary outgoing-message failure must not terminate the bot.
            print("Feishu could not send a reply; the connector is staying online.", file=sys.stderr)
            return False

    async def run(self):
        self.loop = asyncio.get_running_loop()
        while not self.registry.shutdown_requested:
            inbox_task = asyncio.create_task(self.inbox.get())
            changed_task = asyncio.create_task(self.background.changed.wait())
            done, pending = await asyncio.wait({inbox_task, changed_task},
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if inbox_task in done:
                incoming = inbox_task.result()
                self.inbox.task_done()
                if isinstance(incoming, Exception):
                    raise RuntimeError("The Feishu connection stopped.") from incoming
                await self.handle(incoming)
            if changed_task in done:
                self.background.changed.clear()
                await self.deliver_ready()

    async def handle(self, incoming: IncomingMessage):
        identity = incoming.event_id or incoming.message_id
        if not identity or identity in self._seen:
            return
        if len(self._seen_order) == self._seen_order.maxlen:
            self._seen.discard(self._seen_order[0])
        self._seen_order.append(identity)
        self._seen.add(identity)
        if incoming.chat_type != "p2p" or not incoming.open_id:
            return
        state = self.access.authorize(incoming.open_id, incoming.text)
        if state == "paired":
            await self._send(incoming.open_id,
                "ATHENA is paired with this Feishu account. Send a message whenever you need me.")
            return
        if state != "allowed":
            await self._send(incoming.open_id,
                "ATHENA is not paired. Enter the /pair code shown on the ATHENA computer.")
            return
        if incoming.message_type != "text" or not incoming.text.strip():
            await self._send(incoming.open_id, "ATHENA currently accepts text messages only.")
            return
        text = incoming.text.strip()
        command = text.casefold()
        if self.background.approval_model is None:
            self.approval_owner = None
        elif self.approval_owner != incoming.open_id:
            await self._send(incoming.open_id,
                "Another approval is waiting for its original sender. Try again after it is resolved.")
            return
        if command in {"/help", "help"}:
            await self._send(incoming.open_id,
                "Commands: /tasks, /cancel ID, /usage, /help. Normal messages run as background tasks.")
            return
        if command == "/usage":
            usage = self.model.usage_estimate
            await self._send(incoming.open_id,
                f"Session estimate: {usage['requests']} DeepSeek requests, about "
                f"{usage['estimated_input_tokens']} input and {usage['estimated_output_tokens']} output tokens.")
            return
        if command == "/tasks":
            rows = self.background.task_rows()
            status = "No model tasks are running." if not rows else "\n".join(
                f"{row['id']}  {row['status']}: {row['text']}" for row in rows)
            download = self.background.download_status()
            shell = self.background.command_status()
            if download.data["status"] != "none":
                status += "\nDownload: " + download.spoken_text
            if shell.data["status"] != "none":
                status += "\nCommand: " + shell.spoken_text
            await self._send(incoming.open_id, status)
            return
        if command.startswith("/cancel "):
            cancelled = await self.background.cancel(command.split(maxsplit=1)[1])
            await self._send(incoming.open_id,
                "Task cancelled." if cancelled else "Use one valid task ID from /tasks.")
            return
        job = self.background.submit(text, self.memory.context_messages())
        if job is None:
            await self._send(incoming.open_id,
                "Five tasks are already running. Send /tasks or /cancel ID.")
            return
        self.recipients[job.id] = incoming.open_id
        if self.background.approval_model is None:
            self.approval_owner = None
        await self._send(incoming.open_id,
            f"Task accepted [{str(job.id)[:8]}]. You can keep messaging me.")

    async def deliver_ready(self):
        while True:
            item = self.background.next_output()
            if item is None:
                return
            job, acknowledgement = item
            recipient = self.recipients.get(job.id)
            if acknowledgement:
                # Feishu already received an immediate task acknowledgement.
                job.acknowledged = True
                continue
            if recipient:
                pending_approval = job.model._tools._pending is not None
                if pending_approval:
                    self.approval_owner = recipient
                delivered = await self._send_long(
                    recipient, job.reply or "That request returned no answer."
                )
                answer = (job.reply or "").strip()
                if answer and delivered:
                    await self.memory.remember_turn(job.id, job.text, answer)
            self.background.delivered(job)
            self.recipients.pop(job.id, None)

    async def _send_long(self, open_id, text):
        text = text.strip()
        for start in range(0, len(text) or 1, 3500):
            if not await self._send(open_id, text[start:start + 3500] or "(empty response)"):
                return False
        return True

    async def close(self):
        await self.background.cancel_all()


class FeishuTransport:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id, self.app_secret = app_id, app_secret
        self.client = (lark.Client.builder().app_id(app_id).app_secret(app_secret)
                       .log_level(lark.LogLevel.WARNING).build())
        self.gateway: FeishuGateway | None = None

    async def send_text(self, open_id: str, text: str):
        body = (CreateMessageRequestBody.builder().receive_id(open_id).msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .uuid(str(uuid4())).build())
        request = (CreateMessageRequest.builder().receive_id_type("open_id")
                   .request_body(body).build())
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(self.client.im.v1.message.create, request)
            except Exception as error:
                if attempt == 2:
                    raise RuntimeError("Feishu could not be reached for an outgoing message.") from error
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            if not response.success():
                raise RuntimeError(f"Feishu rejected an outgoing message (code {response.code}).")
            return

    @staticmethod
    def parse_event(data: P2ImMessageReceiveV1) -> IncomingMessage | None:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        if message is None or sender_id is None or getattr(sender, "sender_type", "") != "user":
            return None
        text = ""
        if getattr(message, "message_type", "") == "text":
            try:
                text = str(json.loads(message.content or "{}").get("text", ""))
            except (ValueError, TypeError):
                text = ""
        header = getattr(data, "header", None)
        return IncomingMessage(
            event_id=str(getattr(header, "event_id", "") or ""),
            message_id=str(getattr(message, "message_id", "") or ""),
            open_id=str(getattr(sender_id, "open_id", "") or ""),
            chat_type=str(getattr(message, "chat_type", "") or ""),
            message_type=str(getattr(message, "message_type", "") or ""),
            text=text,
        )

    def start_socket_thread(self, gateway: FeishuGateway):
        self.gateway = gateway
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._receive).build())
        def socket_main():
            try:
                # lark-oapi stores its WebSocket event loop at module scope.
                # Give the daemon thread a dedicated loop so it never conflicts
                # with ATHENA's asyncio loop.
                import lark_oapi.ws.client as ws_impl
                ws_impl.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(ws_impl.loop)
                client = lark.ws.Client(self.app_id, self.app_secret,
                    log_level=lark.LogLevel.INFO, event_handler=handler)
                client.start()
            except Exception as error:
                gateway.enqueue_threadsafe(error)
        thread = threading.Thread(target=socket_main, name="athena-feishu", daemon=True)
        thread.start()
        return thread

    def _receive(self, data: P2ImMessageReceiveV1):
        incoming = self.parse_event(data)
        if incoming is not None and self.gateway is not None:
            self.gateway.enqueue_threadsafe(incoming)


def _build():
    load_local_environment()
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    missing = [name for name, value in (("FEISHU_APP_ID", app_id),
        ("FEISHU_APP_SECRET", app_secret), ("DEEPSEEK_API_KEY", deepseek_key)) if not value]
    if missing:
        raise ValueError("Add these values to the project .env file: " + ", ".join(missing))
    settings = RuntimeSettingsStore()
    registry = ToolRegistry.discover(services={"settings": settings})
    model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
    model = DeepSeekLanguageModel(deepseek_key, model_name, registry, settings, interface="text")
    memory = MemoryService(database_path(), deepseek_key, model_name,
        settings.get("memory_batch_delay_seconds"), settings)
    access = FeishuAccess(data_directory() / "feishu_allowed.json",
                          os.environ.get("FEISHU_ALLOWED_OPEN_IDS", ""))
    transport = FeishuTransport(app_id, app_secret)
    gateway = FeishuGateway(model, memory, registry, transport.send_text, access)
    return gateway, transport, memory


async def run():
    gateway, transport, memory = _build()
    await memory.connect()
    if gateway.access.pairing_code:
        print("Feishu pairing code:", gateway.access.pairing_code)
        print(f"Send /pair {gateway.access.pairing_code} to your ATHENA bot in a private Feishu chat.")
    transport.start_socket_thread(gateway)
    print("ATHENA Feishu connector is running. Press Ctrl+C to stop.")
    try:
        await gateway.run()
    finally:
        await gateway.close()
        await asyncio.gather(gateway.model.close(), memory.close(), return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        asyncio.run(run())
        return 0
    except KeyboardInterrupt:
        print("\nATHENA Feishu connector stopped.")
        return 130
    except (ValueError, OSError, RuntimeError) as error:
        print(f"ATHENA Feishu connector failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
