import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from athena.openclaw_tasks import OpenClawTasks, session_progress
from athena.tools.openclaw import OpenClawBridge
from athena.tools.registry import ToolRegistry
from athena.tools.models import ToolResult


class TaskTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_submission_can_be_polled_without_waiting_or_duplicate_start(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = OpenClawTasks(Path(directory)/'tasks.sqlite')
            gate = asyncio.Event()

            async def work(objective, **kwargs):
                await gate.wait()
                return True, 'Created the requested file; test output: OK.', {}

            with patch('athena.openclaw_tasks.run_task', AsyncMock(side_effect=work)) as run, \
                 patch('athena.openclaw_tasks.session_progress', return_value=[{'tool': 'write', 'state': 'returned', 'seq': 3}]), \
                 patch('athena.openclaw_tasks.environment', return_value={}):
                ident = manager.start('Write the file', 'abcdef123456')
                self.assertEqual(manager.start('Write the file', ident), ident)
                self.assertEqual(manager.rows(ident)[0]['state'], 'queued')
                await asyncio.sleep(0)
                self.assertEqual(manager.rows(ident)[0]['state'], 'running')
                self.assertIn('write', manager.rows(ident)[0]['progress'])
                gate.set(); await asyncio.gather(*manager.running)
                row = manager.rows(ident, report=True)[0]
                self.assertEqual(row['state'], 'reply_received')
                self.assertIn('test output: OK', row['report'])
                run.assert_awaited_once()
                manager.db.close()
                reloaded = OpenClawTasks(Path(directory)/'tasks.sqlite')
                self.assertEqual(reloaded.rows(ident, report=True)[0]['report'], row['report'])
                reloaded.db.close()

    async def test_interrupted_worker_is_unconfirmed_and_never_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = OpenClawTasks(Path(directory)/'tasks.sqlite')
            with patch('athena.openclaw_tasks.run_task', AsyncMock(side_effect=TimeoutError)) as run, \
                 patch('athena.openclaw_tasks.session_progress', return_value=[]), \
                 patch('athena.openclaw_tasks.environment', return_value={}):
                ident = manager.start('Fix program')
                await asyncio.gather(*manager.running)
                self.assertEqual(manager.rows(ident, report=True)[0]['state'], 'unconfirmed')
                self.assertEqual(run.await_count, 1)
                manager.db.close()

    async def test_submission_receipt_is_submitted_not_completed(self):
        from athena.tools.openclaw import OpenClawTaskTool
        registry = ToolRegistry()
        tool = OpenClawTaskTool()
        tool.execute = AsyncMock(return_value=ToolResult(True, 'OpenClaw task abcdef123456 queued.',
            {'task_id': 'abcdef123456', 'background_started': True, 'status': 'queued'}))
        registry.register(tool)
        await registry.execute('openclaw_task', {'objective': 'Fix code'}, confirmed=True)
        self.assertEqual(registry.status_store.rows('openclaw_task')[0]['state'], 'submitted')

    def test_progress_reads_only_matching_session_and_omits_sensitive_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root/'state/agents/main/agent/openclaw-agent.sqlite'
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE session_nodes(session_key TEXT,current_session_id TEXT)')
                db.execute('CREATE TABLE transcript_events(session_id TEXT,seq INTEGER,event_json TEXT)')
                db.execute('INSERT INTO session_nodes VALUES (?,?)', ('agent:main:athena-check', 'session1'))
                for sid, seq, message in [
                    ('session1', 1, {'role': 'assistant', 'content': [{'type': 'toolCall', 'name': 'exec', 'arguments': {'password': 'secret'}}]}),
                    ('session1', 2, {'role': 'toolResult', 'toolName': 'exec', 'isError': True, 'content': 'secret output'}),
                    ('other', 3, {'role': 'toolResult', 'toolName': 'PRIVATE_OTHER_TASK'})]:
                    db.execute('INSERT INTO transcript_events VALUES (?,?,?)', (sid, seq, json.dumps({'message': message})))
            db.close()
            with patch('athena.openclaw_tasks.installation', return_value=root):
                events = session_progress('athena-check')
            self.assertEqual(events[0]['state'], 'failed')
            self.assertEqual(events[0]['tool'], 'exec')
            self.assertNotIn('secret', json.dumps(events))
            self.assertNotIn('PRIVATE_OTHER_TASK', json.dumps(events))

    def test_bridge_schema_requires_objective_only_for_start_and_rejects_commands_in_status(self):
        from jsonschema import Draft202012Validator
        schema = Draft202012Validator(OpenClawBridge.definition.parameters)
        self.assertTrue(schema.is_valid({'action': 'start', 'objective': 'Fix code', 'id': 'abcdef123456'}))
        self.assertTrue(schema.is_valid({'action': 'report', 'id': 'abcdef123456'}))
        self.assertFalse(schema.is_valid({'action': 'start'}))
        self.assertFalse(schema.is_valid({'action': 'status', 'objective': 'Run a command'}))
