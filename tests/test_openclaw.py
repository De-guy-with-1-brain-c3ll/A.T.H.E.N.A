import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from athena.openclaw import run_task
from athena.tools.models import PermissionLevel, ToolResult
from athena.tools.openclaw import OpenClawTaskTool
from athena.tools.registry import ToolRegistry


class OpenClawTests(unittest.IsolatedAsyncioTestCase):
    def process(self, body, code=0, stderr=b""):
        process = Mock(returncode=code)
        process.communicate = AsyncMock(return_value=(json.dumps(body).encode(), stderr))
        return process

    async def call(self, process):
        with patch("athena.openclaw.command", return_value=["node", "openclaw.mjs"]), \
             patch("athena.openclaw.environment", return_value={"DEEPSEEK_API_KEY": "private-test-key"}), \
             patch("athena.openclaw.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            result = await run_task("Inspect the workspace")
            self.assertEqual(spawn.await_count, 1)
            self.assertIn("--json", spawn.call_args.args)
            self.assertNotIn("private-test-key", spawn.call_args.args)
            return result

    async def test_gateway_reply_is_parsed_and_secrets_redacted(self):
        success, text, data = await self.call(self.process({"status": "ok", "result": {"payloads": [{"text": "Done private-test-key"}]}}))
        self.assertTrue(success)
        self.assertEqual(text, "Done [redacted]")
        self.assertTrue(data["reply_received"])

    async def test_transport_failure_is_not_retried_or_claimed_complete(self):
        success, text, data = await self.call(self.process({}, 1, b"private-test-key connection lost"))
        self.assertFalse(success)
        self.assertIn("before retrying", text)
        self.assertNotIn("private-test-key", data["diagnostic"])

    async def test_empty_reply_does_not_claim_completion(self):
        success, _, _ = await self.call(self.process({"status": "ok", "result": {"payloads": []}}))
        self.assertFalse(success)

    async def test_delegation_obeys_existing_approval_boundary(self):
        registry = ToolRegistry()
        tool = OpenClawTaskTool()
        self.assertEqual(tool.definition.permission, PermissionLevel.CONFIRM)
        registry.register(tool)
        with patch("athena.openclaw_tasks.run_task", AsyncMock()) as task:
            result = await registry.execute("openclaw_task", {"objective": "Organise files"})
            self.assertTrue(result.data["pending_approval"])
            task.assert_not_awaited()

    async def test_cancellation_reaps_the_cli(self):
        process = Mock(returncode=None)
        process.communicate = AsyncMock(side_effect=[asyncio.CancelledError(), (b"", b"")])
        with patch("athena.openclaw.command", return_value=["node", "openclaw.mjs"]), \
             patch("athena.openclaw.environment", return_value={}), \
             patch("athena.openclaw.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                await run_task("Inspect files")
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 2)

    async def test_approval_starts_openclaw_and_never_uses_download_flow(self):
        registry = ToolRegistry(); tool = OpenClawTaskTool(); registry.register(tool)
        tool.execute = AsyncMock(return_value=ToolResult(True, 'OpenClaw task abcdef123456 queued.',
            {'task_id': 'abcdef123456', 'background_started': True}))
        await registry.execute('openclaw_task', {'objective': 'Fix code'})
        result = await registry.handle_permission_reply('yes')
        self.assertEqual(result.data['task_id'], 'abcdef123456')
        tool.execute.assert_awaited_once_with({'objective': 'Fix code'})
        self.assertFalse(registry._download_tasks)
        self.assertFalse(registry._last_download)
        self.assertEqual(registry.status_store.rows('openclaw_task')[0]['state'], 'submitted')
