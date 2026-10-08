import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer
from athena.pc_transfer import inbox_app, signature
from athena.tools.models import ToolDefinition, ToolResult
from athena.tools.registry import ToolRegistry
from athena.tools.pc_transfer import UploadTool
from athena.workflows import Workflows


class ReadTool:
    definition = ToolDefinition("get_weather", "test", {"type": "object", "properties": {}, "additionalProperties": False})
    def __init__(self): self.calls = 0; self.success = True
    async def execute(self, arguments):
        self.calls += 1
        await asyncio.sleep(0)
        return ToolResult(self.success, "Verified weather." if self.success else "Network failed.", {"temperature": 25})


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = ToolRegistry()
        self.tool = ReadTool()
        self.registry.register(self.tool)
        self.reports = []
        self.manager = Workflows(self.registry, lambda text: self.reports.append(text) or True, Path(self.temp.name) / "tasks.db")
        self.steps = [{"tool": "get_weather", "arguments": {}}] * 3
    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()
    async def test_multistep_and_report(self):
        ident = self.manager.create("Brief", self.steps)
        await self.manager.run(self.manager.claim())
        self.assertEqual(self.manager.rows(ident)[0]["completed_steps"], 3)
        await self.manager.report()
        await self.manager.report()
        self.assertEqual(len(self.reports), 1)
    async def test_failure_stops_following_steps(self):
        self.tool.success = False
        ident = self.manager.create("Brief", self.steps)
        await self.manager.run(self.manager.claim())
        self.assertEqual(self.tool.calls, 1)
        self.assertEqual(self.manager.rows(ident)[0]["state"], "failed")
    async def test_cross_process_claim(self):
        self.manager.create("Brief", self.steps)
        other = Workflows(self.registry, lambda text: True, self.manager.path)
        self.assertIsNotNone(self.manager.claim())
        self.assertIsNone(other.claim())
    async def test_cancel(self):
        ident = self.manager.create("Brief", self.steps)
        row = self.manager.claim()
        self.manager.cancel(ident)
        await self.manager.run(row)
        self.assertEqual(self.tool.calls, 0)
    async def test_recurring(self):
        ident = self.manager.create("Brief", self.steps, 300)
        await self.manager.run(self.manager.claim())
        await self.manager.report()
        self.assertEqual(self.manager.rows(ident)[0]["state"], "queued")
        self.assertIsNone(self.manager.claim())
    async def test_crash_not_replayed(self):
        ident = self.manager.create("Brief", self.steps)
        self.manager.claim()
        with self.manager.db() as db: db.execute("UPDATE jobs SET lease=0")
        self.assertIsNone(self.manager.claim())
        self.assertEqual(self.manager.rows(ident)[0]["state"], "failed")
    async def test_guardrails(self):
        for tool in ("run_command", "upload_to_pc", "background_workflow"):
            with self.assertRaises(ValueError):
                self.manager.create("Unsafe", [{"tool": tool, "arguments": {}}])
        with self.assertRaises(ValueError): self.manager.create("Too fast", self.steps, 1)
    async def test_notification_backoff(self):
        self.manager.notify = lambda text: False
        self.manager.create("Brief", self.steps)
        await self.manager.run(self.manager.claim())
        await self.manager.report()
        with self.manager.db() as db:
            row = db.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(row["notified"], 0)
            self.assertGreater(row["lease"], time.time())


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = "test-key-" * 8
        self.client = TestClient(TestServer(inbox_app(Path(self.temp.name) / "inbox", self.key)))
        await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()
    def headers(self, body=b"hello", name="report.txt", nonce=None):
        stamp, nonce = str(int(time.time())), nonce or uuid4().hex
        digest = hashlib.sha256(body).hexdigest()
        return {"X-Athena-Time": stamp, "X-Athena-Nonce": nonce, "X-Athena-Name": name,
                "X-Athena-SHA256": digest, "X-Athena-Signature": signature(self.key, stamp, nonce, name, digest)}
    async def test_receive_replay_and_no_overwrite(self):
        headers = self.headers()
        response = await self.client.post("/upload", data=b"hello", headers=headers)
        self.assertEqual(response.status, 200)
        receipt = await response.json()
        self.assertEqual((Path(self.temp.name)/"inbox"/receipt["filename"]).read_bytes(), b"hello")
        self.assertEqual((await self.client.post("/upload", data=b"hello", headers=headers)).status, 403)
        self.assertEqual((await self.client.post("/upload", data=b"hello", headers=self.headers())).status, 200)
    async def test_bad_auth_traversal_and_hash(self):
        for name in ("../secret.txt", "CON.txt", "secret.txt:stream"):
            self.assertEqual((await self.client.post("/upload", data=b"hello", headers=self.headers(name=name))).status, 403)
        headers = self.headers(); headers["X-Athena-Signature"] = "invalid"
        self.assertEqual((await self.client.post("/upload", data=b"hello", headers=headers)).status, 403)
        self.assertEqual((await self.client.post("/upload", data=b"changed", headers=self.headers())).status, 400)
        self.assertFalse(list((Path(self.temp.name)/"inbox").glob("*.partial")))
    async def test_requested_transfer_starts_without_extra_approval(self):
        root = Path(self.temp.name)/"data"; (root/"reports").mkdir(parents=True)
        file = root/"reports"/"report.txt"; file.write_text("hello")
        registry = ToolRegistry(); tool = UploadTool(); registry.register(tool)
        with patch.dict(os.environ, {"ATHENA_DATA_DIR": str(root), "ATHENA_PC_TRANSFER_KEY": self.key,
                                     "ATHENA_PC_UPLOAD_URL": str(self.client.make_url("/upload"))}):
            result = await registry.execute("upload_to_pc", {"path": str(file)})
            self.assertTrue(result.success)
            self.assertFalse(list((Path(self.temp.name)/"inbox").glob("*.txt")))
            self.assertFalse(registry.has_pending_approval)
            self.assertTrue(result.data['background_started'])
            await registry.wait_for_commands()
            self.assertIn("Sent", tool.status)
            status = registry.status_store.result(operation_id=result.data['operation_id'])
            self.assertEqual(status.data['status'], 'completed')
    async def test_changed_file_requires_new_approval(self):
        root = Path(self.temp.name)/"data"; (root/"reports").mkdir(parents=True)
        file = root/"reports"/"report.txt"; file.write_text("hello")
        with patch.dict(os.environ, {"ATHENA_DATA_DIR": str(root), "ATHENA_PC_TRANSFER_KEY": self.key,
                                     "ATHENA_PC_UPLOAD_URL": str(self.client.make_url("/upload"))}):
            tool = UploadTool(); prepared, _ = await tool.prepare({"path": str(file)})
            file.write_text("changed")
            self.assertFalse((await tool.execute(prepared)).success)
            with self.assertRaises(ValueError): tool.inspect({"path": str(root/".."/"private.txt")})
