import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from athena.tools.models import ToolResult
from athena.tools.pc_transfer import UploadTool
from athena.tools.registry import ToolRegistry


class RoutineTransferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {'ATHENA_DATA_DIR': temporary.name})
        environment.start()
        self.addCleanup(environment.stop)

    async def test_returns_before_receipt_and_notifies_once(self):
        registry = ToolRegistry()
        tool = UploadTool()
        tool.prepare = AsyncMock(return_value=({'path': 'coding/main.py'}, 'prepared'))
        release = asyncio.Event()
        async def send(arguments):
            await release.wait()
            return ToolResult(True, 'Sent main.py to the PC inbox.')
        tool.execute = send
        tool.notify = AsyncMock()
        registry.register(tool)
        result = await asyncio.wait_for(registry.execute('upload_to_pc', {}), .5)
        self.assertTrue(result.data['background_started'])
        self.assertFalse(registry.has_pending_approval)
        self.assertNotEqual(registry.status_store.result(operation_id=result.data['operation_id']).data['status'], 'completed')
        tool.notify.assert_not_awaited()
        release.set()
        await registry.wait_for_commands()
        self.assertEqual(registry.status_store.result(operation_id=result.data['operation_id']).data['status'], 'completed')
        tool.notify.assert_awaited_once_with('Sent main.py to the PC inbox.')

    async def test_failed_preparation_never_dispatches(self):
        registry = ToolRegistry()
        tool = UploadTool()
        tool.prepare = AsyncMock(side_effect=ValueError('Credentials cannot be sent.'))
        tool.execute = AsyncMock()
        registry.register(tool)
        result = await registry.execute('upload_to_pc', {'path': 'secret.env'})
        self.assertFalse(result.success)
        tool.execute.assert_not_awaited()
        self.assertFalse(registry.has_pending_approval)
        self.assertEqual(registry.status_store.result(operation_id=result.data['operation_id']).data['status'], 'failed')

    async def test_failed_receipt_reports_failure_once(self):
        registry = ToolRegistry()
        tool = UploadTool()
        tool.prepare = AsyncMock(return_value=({'path': 'reports/file.txt'}, 'prepared'))
        tool.execute = AsyncMock(return_value=ToolResult(False, 'PC unreachable.'))
        tool.notify = AsyncMock()
        registry.register(tool)
        result = await registry.execute('upload_to_pc', {})
        await registry.wait_for_commands()
        self.assertEqual(registry.status_store.result(operation_id=result.data['operation_id']).data['status'], 'failed')
        tool.notify.assert_awaited_once_with('PC unreachable.')
