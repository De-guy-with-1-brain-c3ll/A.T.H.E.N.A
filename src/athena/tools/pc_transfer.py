"""User-approved artifact transfer to a configured PC inbox."""
import asyncio
import hashlib
import ipaddress
import os
import re
import json
from pathlib import Path
import time
import inspect
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from athena.paths import data_directory
from athena.pc_transfer import MAX_BYTES, signature
from athena.tools.coding import check_link
from athena.tools.models import PermissionLevel, ToolDefinition, ToolResult


class UploadTool:
    def __init__(self):
        self.notify = None
        self.coding = None

    @property
    def status(self):
        try:
            return json.loads((data_directory() / "pc-transfer-status.json").read_text())["message"]
        except (OSError, ValueError, KeyError):
            return "No PC transfer recorded."

    @status.setter
    def status(self, message):
        from athena.alerts import _FileLock
        root = data_directory()
        root.mkdir(parents=True, exist_ok=True)
        target = root / "pc-transfer-status.json"
        with _FileLock(root / "pc-transfer-status.lock"):
            temporary = root / f".pc-transfer-{uuid4().hex}.tmp"
            try:
                temporary.write_text(json.dumps({"message": message, "updated": time.time()}), encoding="utf-8")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    def bind(self, services):
        scheduler = services.get("alert_scheduler")
        self.notify = (lambda text: scheduler.notify(text)) if scheduler else None

    async def finish(self, arguments):
        try:
            result = await self.execute(arguments)
            self.status = result.spoken_text
            if self.notify:
                pending = self.notify(self.status)
                if inspect.isawaitable(pending):
                    await pending
            return result
        except asyncio.CancelledError:
            self.status = "File transfer interrupted; check the PC inbox before retrying."
            raise
        except Exception:
            self.status = "File transfer reporting failed; check the PC inbox."
            return ToolResult(False, self.status)
    definition = ToolDefinition(name="upload_to_pc",
        description="Send a generated artifact or downloaded file to the user's configured PC inbox. Requires fresh user approval. Only files under ATHENA data/coding, data/downloads or data/reports; never credentials. Maximum 25 MiB. Destination cannot be supplied by the model.",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 500,
                     "description": "Verified file path. Omit to send the last file actually written in this interface."}},
            "additionalProperties": False},
        permission=PermissionLevel.CONFIRM, timeout_seconds=90)

    def inspect(self, arguments):
        key = os.environ.get("ATHENA_PC_TRANSFER_KEY", "")
        url = os.environ.get("ATHENA_PC_UPLOAD_URL", "")
        parsed = urlsplit(url)
        if len(key) < 32 or parsed.scheme not in {"http", "https"} or parsed.path != "/upload" or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Configure ATHENA_PC_UPLOAD_URL and ATHENA_PC_TRANSFER_KEY first.")
        if not ipaddress.ip_address(parsed.hostname).is_private:
            raise ValueError("The destination must be a private LAN IP address.")
        root = data_directory().absolute()
        requested = arguments.get("path") or getattr(self.coding, "last_artifact", None)
        if not requested:
            raise ValueError("No saved file selected. Create a file first or specify its verified path.")
        candidate = Path(requested)
        candidate = candidate if candidate.is_absolute() else root / candidate
        candidate = candidate.absolute()
        for ancestor in (*candidate.parents, candidate):
            check_link(ancestor)
        path = candidate.resolve()
        if not any(path.is_relative_to((root / folder).resolve()) for folder in ("coding", "downloads", "reports")):
            raise ValueError("Only coding, download and report artifacts may be sent.")
        if any(part.startswith(".") for part in path.relative_to(root.resolve()).parts) or path.suffix.lower() in {".pem", ".key", ".env", ".sqlite3", ".db"}:
            raise ValueError("Hidden files and credentials cannot be sent.")
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError("File is missing or exceeds 25 MiB.")
        payload = path.read_bytes()
        return path, payload, url, key

    async def prepare(self, arguments):
        path, payload, url, _ = await asyncio.to_thread(self.inspect, arguments)
        digest = hashlib.sha256(payload).hexdigest()
        return {"path": str(path), "sha256": digest, "destination": url}, f"Send {path.name} ({len(payload)} bytes) to your PC at {urlsplit(url).hostname}? Say yes or no."

    async def execute(self, arguments):
        try:
            path, payload, url, key = await asyncio.to_thread(self.inspect, arguments)
            digest = hashlib.sha256(payload).hexdigest()
            if digest != arguments.get("sha256") or url != arguments.get("destination"):
                return ToolResult(False, "The file or destination changed. Request fresh approval.")
            stamp, nonce = str(int(time.time())), uuid4().hex
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", path.name)[:115]
            name = "artifact-" + name if not name[:1].isalnum() else name
            name = name[:120].rstrip(".")
            headers = {"X-Athena-Time": stamp, "X-Athena-Nonce": nonce,
                       "X-Athena-Name": name, "X-Athena-SHA256": digest,
                       "X-Athena-Signature": signature(key, stamp, nonce, name, digest)}
            from athena.metrics import progress
            started = time.monotonic()
            headers['Content-Length'] = str(len(payload))
            async def chunks():
                last_update = 0
                for offset in range(0, len(payload), 65536):
                    part = payload[offset:offset + 65536]
                    yield part
                    done = offset + len(part)
                    now = time.monotonic()
                    if now - last_update < .25 and done != len(payload):
                        continue
                    last_update = now
                    progress('transfer', {'state': 'sending', 'filename': path.name,
                        'bytes_done': done, 'bytes_total': len(payload),
                        'bytes_per_second': done / max(.001, time.monotonic() - started),
                        'message': 'Bytes submitted to network; awaiting verified PC receipt.'})
            progress('transfer', {'state': 'sending', 'filename': path.name,
                'bytes_done': 0, 'bytes_total': len(payload)})
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=80), trust_env=False) as session:
                async with session.post(url, data=chunks(), headers=headers, allow_redirects=False) as response:
                    if response.status != 200:
                        return ToolResult(False, f"PC transfer failed (HTTP {response.status}).")
                    receipt = await response.json()
                    if receipt.get("sha256") != digest or receipt.get("bytes") != len(payload):
                        return ToolResult(False, "The PC did not confirm the complete file.")
            progress('transfer', {'state': 'complete', 'filename': path.name,
                'bytes_done': len(payload), 'bytes_total': len(payload),
                'message': 'PC verified the complete file.', 'receipt': receipt})
            return ToolResult(True, f"Sent {path.name} to your PC's ATHENA inbox.", receipt)
        except (ValueError, OSError, aiohttp.ClientError, TimeoutError):
            from athena.metrics import progress
            progress('transfer', {'state': 'failed', 'message': 'No verified complete receipt.'})
            return ToolResult(False, "Could not send the file. Check the PC receiver and LAN connection.")


class TransferStatus:
    definition = ToolDefinition(name="pc_transfer_status", description="Check the last PC file transfer across ATHENA interfaces.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False})
    def __init__(self, upload):
        self.upload = upload
        self.store = None
    async def execute(self, arguments):
        if self.store is not None:
            return self.store.result('upload_to_pc')
        return ToolResult(True, self.upload.status)


def create_tools():
    upload = UploadTool()
    return [upload, TransferStatus(upload)]
