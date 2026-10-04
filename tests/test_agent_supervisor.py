import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from athena.agents import AgentSupervisor, AgentRegistry
from athena.tools.agents import AgentTaskTool
from athena.tools.models import ToolDefinition, ToolResult
from athena.tools.registry import ToolRegistry


class EvidenceTool:
    definition = ToolDefinition('get_weather', 'test', {'type': 'object'})
    async def execute(self, arguments):
        return ToolResult(True, 'Verified weather.')


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = ToolRegistry(); self.registry.register(EvidenceTool())
        self.reports = []
        self.manager = AgentSupervisor(self.registry, SimpleNamespace(),
            lambda report: self.reports.append(report) or True, Path(self.temp.name)/'agents.db')

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    def spawn(self, goal='Check weather', **kwargs):
        return self.manager.spawn(goal, ['get_weather'], **kwargs)

    async def test_spawn_returns_before_model_and_global_claim_limit(self):
        tool = AgentTaskTool(self.manager)
        self.manager.make_model = lambda registry: (_ for _ in ()).throw(AssertionError('must not launch inline'))
        result = await tool.execute({'action':'spawn','objective':'Check weather','tools':['get_weather']})
        self.assertTrue(result.success)
        self.spawn(); self.spawn()
        other = AgentSupervisor(self.registry, None, lambda report: True, self.manager.path)
        self.assertIsNotNone(self.manager.claim()); self.assertIsNotNone(other.claim())
        self.assertIsNone(other.claim())

    async def test_children_inherit_capabilities_and_depth_is_bounded(self):
        parent = self.spawn()
        child = self.manager.spawn('Child', parent=parent)
        grandchild = self.manager.spawn('Grandchild', parent=child)
        with self.assertRaises(ValueError): self.manager.spawn('Too deep', parent=grandchild)
        for name in ('run_command', 'upload_to_pc', 'download_file', 'manage_settings'):
            with self.assertRaises(ValueError): self.manager.spawn('Unsafe', [name], parent)

    async def test_cancel_subtree_without_affecting_other_roots(self):
        root = self.spawn(); child = self.manager.spawn('Child', parent=root); other = self.spawn()
        self.manager.cancel(root)
        self.assertEqual(self.manager.rows(child)[0]['state'], 'cancelled')
        self.assertEqual(self.manager.rows(other)[0]['state'], 'queued')

    async def test_budget_shared_across_children_and_idle_never_spends(self):
        root = self.spawn(budget=4000)
        child = self.manager.spawn('Child', parent=root)
        job = self.manager.claim(); other = self.manager.claim()
        self.assertTrue(self.manager.authorize_request(job, 1500, 1500))
        self.assertFalse(self.manager.authorize_request(other, 1500, 1500))
        self.assertEqual(self.manager.rows(root)[0]['requests'], 1)
        self.assertEqual(self.manager.rows(child)[0]['budget'], 4000)

    async def test_verified_completion_and_report_once(self):
        class Model:
            async def stream_reply(model, ident, objective, context):
                result = await model.registry.execute('get_weather', {})
                yield json.dumps({'state':'complete','report':'Verified weather.', 'evidence':[result.data['operation_id']]})
        def factory(registry):
            model = Model(); model.registry = registry; return model
        self.manager.model_factory = factory
        ident = self.spawn()
        await self.manager.run(self.manager.claim())
        self.assertEqual(self.manager.rows(ident)[0]['state'], 'complete')
        await self.manager.report(); await self.manager.report()
        self.assertEqual(len(self.reports), 1)

    async def test_fake_evidence_is_rejected(self):
        class Model:
            async def stream_reply(model, *args):
                yield '{"state":"complete","report":"Done","evidence":["invented"]}'
        self.manager.model_factory = lambda registry: Model()
        ident = self.spawn(); await self.manager.run(self.manager.claim())
        self.assertEqual(self.manager.rows(ident)[0]['state'], 'failed')

    async def test_promises_without_evidence_are_blocked(self):
        class Model:
            async def stream_reply(model, *args):
                yield '{"state":"complete","report":"I will do that.","evidence":[]}'
        self.manager.model_factory = lambda registry: Model()
        ident = self.spawn(); await self.manager.run(self.manager.claim())
        self.assertEqual(self.manager.rows(ident)[0]['state'], 'blocked')

    async def test_steering_is_consumed_once(self):
        ident = self.spawn()
        self.manager.steer(ident, 'Focus on Shenzhen')
        self.assertEqual(self.manager.take_notes(ident), ['Focus on Shenzhen'])
        self.assertEqual(self.manager.take_notes(ident), [])

    async def test_child_cannot_cancel_sibling_or_parent(self):
        root = self.spawn(); child = self.manager.spawn('Child', parent=root)
        tool = AgentTaskTool(self.manager, parent=child)
        result = await tool.execute({'action':'cancel','id':root})
        self.assertFalse(result.success)
        self.assertEqual(self.manager.rows(root)[0]['state'], 'queued')

    async def test_crash_not_replayed(self):
        ident = self.spawn(); self.manager.claim()
        with self.manager.db() as db: db.execute('UPDATE agents SET lease=0 WHERE id=?', (ident,))
        self.manager.claim()
        self.assertEqual(self.manager.rows(ident)[0]['state'], 'failed')

    async def test_child_reports_wake_parent_without_model_polling(self):
        root = self.spawn(); self.manager.claim(); child = self.manager.spawn('Child', parent=root)
        with self.manager.db() as db: db.execute("UPDATE agents SET state='waiting' WHERE id=?", (root,))
        self.assertEqual(self.manager.claim()['id'], child)
        with self.manager.db() as db: db.execute("UPDATE agents SET state='complete' WHERE id=?", (child,))
        self.assertEqual(self.manager.claim()['id'], root)

    async def test_shutdown_releases_workers_and_marks_interrupted(self):
        started = asyncio.Event()
        class Model:
            async def stream_reply(model, *args):
                started.set(); await asyncio.Event().wait(); yield ''
        self.manager.model_factory = lambda registry: Model()
        ident = self.spawn(); await self.manager.start()
        await asyncio.wait_for(started.wait(), 1)
        await self.manager.close()
        self.assertFalse(self.manager.workers)
        self.assertEqual(self.manager.rows(ident)[0]['state'], 'failed')

    async def test_notification_retry_recovers_abandoned_lease(self):
        ident = self.spawn()
        with self.manager.db() as db:
            db.execute("UPDATE agents SET state='complete', report='Verified', notified=-1, lease=0 WHERE id=?", (ident,))
        await self.manager.report()
        self.assertEqual(len(self.reports), 1)

    async def test_restricted_registry_blocks_unadvertised_tools(self):
        self.spawn(); registry = AgentRegistry(self.manager, self.manager.claim())
        with self.assertRaises(ValueError): await registry.execute('shutdown_athena', {}, confirmed=True)

    async def test_finished_child_tool_does_not_finish_root_task(self):
        root = self.spawn('Research weather and write a report')
        child = self.manager.spawn('Check weather', parent=root)
        self.registry.register(AgentTaskTool(self.manager))
        result = await self.registry.execute('get_weather', {})
        with self.manager.db() as db:
            db.execute('UPDATE agents SET evidence=? WHERE id=?', (json.dumps([
                {'id':result.data['operation_id'],'tool':'get_weather','success':True}]), child))
        status = self.registry.contextual_status('Is it finished?', [
            {'role':'user','content':'Research weather and write a report'}])
        self.assertEqual(status.data['agent']['id'], root)
        self.assertEqual(status.data['agent']['state'], 'queued')

    async def test_parent_delegates_waits_and_consolidates_verified_child(self):
        class Model:
            async def stream_reply(model, *args):
                registry = model.registry
                if registry.job['depth'] == 0:
                    children = registry.manager.rows(parent=registry.job['id'])
                    if not children:
                        await registry.execute('agent_task', {'action':'spawn','objective':'Verify weather'})
                        yield '{"state":"waiting","report":"Child queued","evidence":[]}'
                    else:
                        evidence = [e['id'] for e in children[0]['evidence'] if e['success']]
                        yield json.dumps({'state':'complete','report':'Child verified weather.', 'evidence':evidence})
                else:
                    result = await registry.execute('get_weather', {})
                    yield json.dumps({'state':'complete','report':'Verified weather.', 'evidence':[result.data['operation_id']]})
        def factory(registry):
            model = Model(); model.registry = registry; return model
        self.manager.model_factory = factory
        root = self.spawn()
        await self.manager.run(self.manager.claim())
        self.assertEqual(self.manager.rows(root)[0]['state'], 'waiting')
        await self.manager.run(self.manager.claim())
        await self.manager.run(self.manager.claim())
        row = self.manager.rows(root)[0]
        self.assertEqual(row['state'], 'complete')
        self.assertTrue(row['evidence'])
        await self.manager.report()
        self.assertEqual(len(self.reports), 1)

    async def test_dashboard_controls_require_auth_and_csrf(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from athena.web import DashboardState, agent_control
        from athena.web_auth import COOKIE, SessionAuth
        state = DashboardState.__new__(DashboardState)
        state.auth = SessionAuth('test password', b'long enough test secret for session signing')
        self.registry.register(AgentTaskTool(self.manager))
        state.session = SimpleNamespace(registry=self.registry)
        app = web.Application(); app['state'] = state
        app.router.add_get('/api/agents', agent_control); app.router.add_post('/api/agents', agent_control)
        async with TestClient(TestServer(app)) as client:
            self.assertEqual((await client.get('/api/agents')).status, 401)
            token = state.issue_session(); client.session.cookie_jar.update_cookies({COOKIE:token})
            self.assertEqual((await client.get('/api/agents')).status, 200)
            self.assertEqual((await client.post('/api/agents', json={'action':'spawn'})).status, 403)
            headers = {'X-ATHENA-CSRF':state.csrf(token)}
            response = await client.post('/api/agents', json={'action':'spawn','objective':'Verify weather','tools':['get_weather']}, headers=headers)
            self.assertTrue((await response.json())['success'])
            invalid = await client.post('/api/agents', json={'action':'spawn','objective':'Unsafe','tools':['run_command']}, headers=headers)
            self.assertEqual(invalid.status, 400)

    async def test_real_adapter_stops_before_paid_request_when_budget_denied(self):
        from athena.llm.deepseek import DeepSeekLanguageModel
        from athena.settings.store import RuntimeSettingsStore
        from uuid import uuid4
        self.spawn(); registry = AgentRegistry(self.manager, self.manager.claim())
        model = DeepSeekLanguageModel('test', 'test', registry,
            RuntimeSettingsStore(Path(self.temp.name)/'settings.json'), interface='agent')
        model._system_prompt = 'Return a JSON report.'
        model._agent_request_budget = lambda estimate, output: False
        model._agent_notes = lambda: []
        model._client.chat.completions.create = AsyncMock()
        try:
            output = ''.join([part async for part in model.stream_reply(uuid4(), 'Verify weather')])
            self.assertEqual(json.loads(output)['state'], 'blocked')
            model._client.chat.completions.create.assert_not_awaited()
        finally:
            await model._client.close()

    async def test_adapter_preserves_json_instead_of_conversation_filters(self):
        from athena.llm.deepseek import DeepSeekLanguageModel
        from athena.settings.store import RuntimeSettingsStore
        from uuid import uuid4
        class Stream:
            async def __aiter__(self):
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                    content='{"state":"blocked","report":"No verified source","evidence":[]}', tool_calls=None))])
        self.spawn(); registry = AgentRegistry(self.manager, self.manager.claim())
        model = DeepSeekLanguageModel('test', 'test', registry,
            RuntimeSettingsStore(Path(self.temp.name)/'settings.json'), interface='agent')
        model._system_prompt = 'Return JSON only.'
        model._agent_request_budget = lambda estimate, output: True
        model._agent_notes = lambda: []
        model._client.chat.completions.create = AsyncMock(return_value=Stream())
        try:
            output = ''.join([part async for part in model.stream_reply(uuid4(), 'Check weather status and report')])
            self.assertEqual(json.loads(output)['state'], 'blocked')
            model._client.chat.completions.create.assert_awaited_once()
        finally:
            await model._client.close()

