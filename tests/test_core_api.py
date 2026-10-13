import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from aiohttp.test_utils import TestClient, TestServer
from athena.core import Core, make_app


class CoreAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'state').mkdir()
        (self.root / 'state/openclaw.json').write_text(json.dumps({'gateway': {'auth': {'token': 'test-only-secret-token'}}}))
        self.core = Core(self.root)
        self.client = TestClient(TestServer(make_app(self.core)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer ' + self.core.token}

    async def asyncTearDown(self):
        await self.client.close()
        await self.core.close()
        self.directory.cleanup()

    async def create(self, **values):
        body = {'objective': 'Create a bounded test artifact', 'cwd': str(self.root),
                'worker': 'openclaw', 'request_id': 'one', 'approval':'required', **values}
        response = await self.client.post('/v1/tasks', json=body, headers=self.headers)
        return response, await response.json()

    async def test_auth_and_scope_approval_are_required(self):
        self.assertEqual((await self.client.get('/v1/tasks')).status, 401)
        response, row = await self.create()
        self.assertEqual(response.status, 200)
        self.assertEqual(row['state'], 'awaiting_approval')
        _, duplicate = await self.create(objective='Do not run this different objective')
        self.assertEqual(duplicate['id'], row['id'])
        self.assertEqual(duplicate['objective'], row['objective'])
        denied = await self.client.post('/v1/tasks/' + row['id'], json={'action': 'deny'}, headers=self.headers)
        self.assertEqual((await denied.json())['state'], 'denied')

    async def test_routine_tasks_start_once_without_scope_approval(self):
        with patch.object(self.core,'launch') as launch:
            _, row=await self.create(objective='Create folder test with a small text file',approval=None)
            self.assertEqual(row['approval_policy'],'routine-auto')
            self.assertIsNone(row['approval_id'])
            await self.create(objective='Do not duplicate',approval=None)
            launch.assert_called_once_with(row['id'])
        self.assertFalse(any(e['type']=='approval' for e in self.core.store.events(row['id'])))

    async def test_sensitive_tasks_still_require_scope_approval(self):
        with patch.object(self.core,'launch') as launch:
            _,row=await self.create(objective='Delete a folder on the desktop',approval=None)
            self.assertEqual(row['state'],'awaiting_approval')
            launch.assert_not_called()

    async def test_workspace_escape_is_rejected_before_task_creation(self):
        response, _ = await self.create(checks=[{'kind': 'python', 'path': '../outside.py'}])
        self.assertEqual(response.status, 400)
        self.assertEqual(self.core.store.rows(), [])

    async def test_desktop_mutation_has_no_unapproved_scope(self):
        response = await self.client.post('/v1/tool', headers=self.headers,
            json={'tool': 'athena_desktop', 'session': 'agent:athena:voice',
                  'arguments': {'action': 'type', 'window': 1, 'text': 'must not type'}})
        self.assertEqual(response.status, 400)

    async def test_windows_case_variants_cannot_access_credentials_or_profiles(self):
        _, row = await self.create()
        with patch.object(self.core, 'scope', AsyncMock(return_value=row)):
            for path in ('.ENV.production', 'User.md', 'MEMORY/old-notes.md'):
                response = await self.client.post('/v1/tool', headers=self.headers,
                    json={'tool':'athena_files','session':row['worker_session'],
                          'arguments':{'action':'read','path':path}})
                self.assertEqual(response.status, 400)

    async def test_target_activation_waits_for_separate_approval_and_denial_sends_no_input(self):
        _, row = await self.create()
        with patch.object(self.core, 'scope', AsyncMock(return_value=row)), patch('athena.desktop_service.execute', AsyncMock()) as desktop:
            waiting = asyncio.create_task(self.client.post('/v1/tool', headers=self.headers,
                json={'tool':'athena_desktop', 'session':row['worker_session'],
                      'arguments':{'action':'click', 'window':1, 'x':10, 'y':10}}))
            for _ in range(50):
                if self.core.approvals: break
                await asyncio.sleep(.01)
            self.assertTrue(self.core.approvals)
            await self.core.decision(next(iter(self.core.approvals)), False)
            response = await waiting
            self.assertEqual(response.status, 400)
            desktop.assert_not_awaited()

    async def test_uncertain_task_is_not_replayed(self):
        _, row = await self.create()
        self.core.store.update(row['id'], state='unconfirmed', run_id='unknown')
        with patch.object(self.core.gateway, 'rpc', AsyncMock(side_effect=ValueError('Unknown run'))):
            response = await self.client.post('/v1/tasks/' + row['id'], headers=self.headers, json={'action': 'resume'})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.core.running, {})

    async def test_reconciliation_cannot_claim_another_runs_report(self):
        _, row = await self.create()
        self.core.store.update(row['id'], state='unconfirmed', run_id='expected-run')
        rpc = AsyncMock(side_effect=[{'status':'ok'}, {'messages':[
            {'role':'assistant','content':'Different run succeeded','__openclaw':{'runId':'other-run'}}]}])
        with patch.object(self.core.gateway, 'rpc', rpc):
            with self.assertRaises(ValueError): await self.core.reconcile(row['id'])
        self.assertEqual(self.core.store.get(row['id'])['state'], 'unconfirmed')

    async def test_verification_failure_preserves_report_and_terminal_state(self):
        _, row = await self.create(checks=[{'kind': 'python', 'path': 'missing.py'}])
        self.core.store.update(row['id'], report='Worker response', state='reply_received')
        await self.core.verify_task(row['id'])
        result = self.core.store.get(row['id'])
        self.assertEqual(result['state'], 'reply_received')
        self.assertEqual(result['report'], 'Worker response')
        self.assertFalse(result['verification'][0]['passed'])

    async def test_disconnected_chat_releases_session_without_stopping_workers(self):
        accepted = asyncio.Event()
        cancelled = asyncio.Event()
        async def stream(*args, **kwargs):
            yield {'type': 'accepted', 'run_id': 'conversation-only'}
            accepted.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(self.core.gateway, 'stream', stream), patch.object(self.core.gateway, 'rpc', AsyncMock()) as rpc:
            response = await self.client.post('/v1/chat', headers=self.headers,
                json={'text': 'hello', 'session': 'disconnect-test'})
            await response.content.readline()
            response.close()
            await asyncio.wait_for(cancelled.wait(), 2)
            await asyncio.sleep(.1)
            rpc.assert_awaited_once_with('chat.abort', {'sessionKey': 'agent:athena:disconnect-test',
                'runId': 'conversation-only', 'preserveSideRuns': True}, timeout=5)
            self.assertFalse(self.core.chat_locks['disconnect-test'].locked())

    async def test_denied_task_cannot_resume_without_scope_approval(self):
        _, row = await self.create()
        self.core.store.update(row['id'], state='denied')
        response = await self.client.post('/v1/tasks/' + row['id'], headers=self.headers, json={'action': 'resume'})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.core.running, {})

    async def test_independent_python_check_reports_real_output(self):
        (self.root / 'test.py').write_text("print('VERIFIED')")
        result = await self.core.verify({'cwd': str(self.root), 'checks': [
            {'kind': 'python', 'path': 'test.py', 'stdout': 'VERIFIED'}]})
        self.assertTrue(result[0]['passed'])
        self.assertEqual(result[0]['exit_code'], 0)

    async def test_cross_interface_approval_targets_exact_id(self):
        _, row = await self.create(session='voice')
        with patch.object(self.core, 'launch') as launch:
            response = await self.client.post('/v1/tasks/' + row['id'], headers=self.headers, json={'action': 'approve'})
            self.assertEqual(response.status, 200)
            launch.assert_called_once_with(row['id'])

    async def test_slash_approval_in_chat_never_reaches_gateway_command_parser(self):
        _, row = await self.create(session='console-stable')
        with patch.object(self.core,'launch') as launch, patch.object(self.core.gateway,'stream') as stream:
            response=await self.client.post('/v1/chat',headers=self.headers,
                json={'text':'/approve '+row['id'],'session':'console-stable','mode':'text'})
            self.assertIn('Approved',(await response.text()))
            launch.assert_called_once_with(row['id'])
            stream.assert_not_called()

    async def test_task_tool_uses_actual_console_workspace(self):
        self.core.session_workspaces['console-stable']=str(self.root)
        response=await self.client.post('/v1/tool',headers=self.headers,
            json={'tool':'athena_tasks','session':'agent:athena:console-stable',
                  'arguments':{'action':'start','objective':'Create a folder'}})
        row=await response.json()
        self.assertEqual(row['cwd'],str(self.root.resolve()))
        self.assertEqual(row['session'],'console-stable')

    async def test_dashboard_shows_streaming_text_and_durable_final_conversation(self):
        gate=asyncio.Event()
        async def stream(*args,**kwargs):
            yield {'type':'accepted','run_id':'dashboard-run'}
            yield {'type':'text','text':'On '}
            await gate.wait()
            yield {'type':'text','text':'it.'}
            yield {'type':'final'}
        with patch.object(self.core.gateway,'stream',stream):
            response=await self.client.post('/v1/chat',headers=self.headers,
                json={'text':'hello','session':'dashboard','mode':'text'})
            await response.content.readline();await response.content.readline()
            live=await (await self.client.get('/v1/dashboard',headers=self.headers)).json()
            self.assertEqual(live['active_turns'][0]['text'],'On ')
            self.assertEqual(live['conversation'][-1]['text'],'hello')
            gate.set();await response.text()
            finished=await (await self.client.get('/v1/dashboard',headers=self.headers)).json()
            self.assertEqual(finished['conversation'][-1]['text'],'On it.')
