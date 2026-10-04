import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
from unittest.mock import patch

from athena.tools.models import ToolDefinition, ToolResult
from athena.tools.registry import ToolRegistry
from athena.tools.status import ToolStatus


class FakeTool:
    def __init__(self, name, result=None, event=None):
        self.definition = ToolDefinition(name, 'test', {'type': 'object'}, timeout_seconds=1)
        self.result = result or ToolResult(True, 'Verified result.')
        self.event = event

    async def execute(self, arguments):
        if self.event:
            await self.event.wait()
        return self.result


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_tool_has_receipt_and_lookup_by_id(self):
        registry = ToolRegistry()
        for name in ('get_weather', 'coding_workspace', 'search_web', 'manage_vpn'):
            registry.register(FakeTool(name))
            result = await registry.execute(name, {})
            status = await registry.execute('check_tool_status', {'operation_id': result.data['operation_id']})
            self.assertEqual(status.data['status'], 'completed')
            self.assertEqual(status.data['operation']['tool'], name)

    async def test_running_and_failure_shared_by_forks(self):
        registry = ToolRegistry(); event = asyncio.Event()
        registry.register(FakeTool('upload_to_pc', ToolResult(False, 'PC unreachable.'), event))
        job = asyncio.create_task(registry.execute('upload_to_pc', {}, confirmed=True))
        await asyncio.sleep(0)
        fork = registry.fork()
        self.assertEqual(fork.contextual_status('Is it sent?').data['status'], 'running')
        event.set(); await job
        status = fork.contextual_status('Is the transfer finished?')
        self.assertEqual(status.data['status'], 'failed')
        self.assertIn('PC unreachable', status.spoken_text)

    async def test_pronoun_refers_to_coding_not_old_download(self):
        registry = ToolRegistry()
        registry._last_download.update(status='complete')
        registry.register(FakeTool('coding_workspace'))
        await registry.execute('coding_workspace', {'action': 'write', 'path': 'main.py'})
        status = await registry.handle_user_command('Is it finished?', [
            {'role': 'user', 'content': 'Write a hello world Python program'}])
        self.assertEqual(status.data['operation']['tool'], 'coding_workspace')
        self.assertFalse(registry.is_download_status_query('Is it finished?'))

    async def test_promised_transfer_is_not_running(self):
        registry = ToolRegistry()
        status = await registry.handle_user_command('Have you sent the file?', [
            {'role': 'assistant', 'content': 'The file is being sent.'}])
        self.assertEqual(status.data['status'], 'none')
        self.assertNotIn('in progress', status.spoken_text)

    async def test_approval_is_not_completion(self):
        registry = ToolRegistry()
        registry.register(FakeTool('protected_action', ToolResult(False, 'Approve first.', {'approval_required': True})))
        await registry.execute('protected_action', {})
        self.assertEqual(registry.status_store.result('protected_action').data['status'], 'waiting_approval')

    async def test_cancelled_call_not_left_running(self):
        registry = ToolRegistry(); event = asyncio.Event()
        registry.register(FakeTool('search_web', event=event))
        job = asyncio.create_task(registry.execute('search_web', {}))
        await asyncio.sleep(0); job.cancel()
        with self.assertRaises(asyncio.CancelledError): await job
        self.assertEqual(registry.status_store.result('search_web').data['status'], 'cancelled')

    async def test_receipts_survive_new_interface_and_do_not_store_code(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status.db'
            store = ToolStatus(path)
            ident = store.begin('coding_workspace', {'path': 'main.py', 'content': 'SECRET'}, 10)
            store.finish(ident, 'completed', 'main.py saved.')
            other = ToolStatus(path)
            self.assertEqual(other.result('coding_workspace').data['status'], 'completed')
            self.assertNotIn('SECRET', str(other.rows()))
            other.db.close(); store.db.close()

    async def test_stale_running_record_never_means_done(self):
        store = ToolStatus()
        with patch('athena.tools.status.time.time', return_value=100):
            store.begin('upload_to_pc', {}, 10)
        self.assertEqual(store.result().data['status'], 'unconfirmed')

    async def test_status_poll_does_not_replace_actual_task(self):
        registry = ToolRegistry()
        registry.register(FakeTool('coding_workspace'))
        await registry.execute('coding_workspace', {'action': 'write'})
        await registry.execute('check_tool_status', {})
        self.assertEqual(registry.contextual_status('Are you done?').data['operation']['tool'], 'coding_workspace')

    async def test_generic_yes_remains_conversation(self):
        self.assertIsNone(await ToolRegistry().handle_user_command('yes'))

    async def test_transfer_approval_and_receipt_share_id_notification_failure_is_separate(self):
        registry = ToolRegistry()
        tool = FakeTool('upload_to_pc', ToolResult(True, 'Sent main.py to PC.', {'bytes': 20}))
        tool.notify = AsyncMock(side_effect=RuntimeError('notification failed'))
        tool.status = ''
        registry.register(tool)
        operation = registry.status_store.begin('upload_to_pc', {'path': 'main.py'}, 90)
        registry.status_store.finish(operation, 'waiting_approval')
        registry._pending_operation = operation
        import time
        registry._pending = ('upload_to_pc', {}, time.monotonic() + 120)
        reply = await registry.handle_user_command('yes')
        self.assertIn('background', reply.spoken_text)
        await asyncio.gather(*list(registry._command_tasks))
        status = registry.status_store.result(operation_id=operation)
        self.assertEqual(status.data['status'], 'completed')
        self.assertIn('Sent main.py', status.spoken_text)

    async def test_clearing_approval_records_not_started(self):
        registry = ToolRegistry()
        registry.register(FakeTool('protected_action', ToolResult(False, 'Approve.', {'approval_required': True})))
        await registry.execute('protected_action', {})
        registry.clear_approval()
        self.assertEqual(registry.status_store.result('protected_action').data['status'], 'cancelled')

    async def test_concurrent_ambiguous_question_asks_which(self):
        from athena.background import BackgroundAgents
        background = BackgroundAgents(SimpleNamespace(_tools=ToolRegistry()))
        background.jobs = {'a': SimpleNamespace(task=SimpleNamespace(done=lambda: False), text='write Python program'),
                           'b': SimpleNamespace(task=SimpleNamespace(done=lambda: False), text='download installer')}
        self.assertIn('Which task', background.contextual_status('Is it finished?').spoken_text)

    async def test_actual_receipt_wins_over_model_still_formulating_reply(self):
        from athena.background import BackgroundAgents
        registry = ToolRegistry()
        background = BackgroundAgents(SimpleNamespace(_tools=registry))
        operation = registry.status_store.begin('upload_to_pc', {'path': 'main.py'}, 90)
        registry.status_store.finish(operation, 'completed', 'Sent main.py to PC.')
        background.jobs = {'a': SimpleNamespace(task=SimpleNamespace(done=lambda: False),
            text='send file to computer', model=SimpleNamespace(_tools=registry), created_at=0)}
        self.assertEqual(background.contextual_status('Is it sent?').data['status'], 'completed')

