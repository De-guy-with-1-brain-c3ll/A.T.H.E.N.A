import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from aiohttp.test_utils import TestClient, TestServer
from athena.pc_desktop import PCDesktop
from athena.pc_transfer import inbox_app, signature
from athena.tools.models import ToolResult
from athena.tools.openclaw import OpenClawTaskTool, create_tools
from athena.tools.pc_desktop import PCDesktopTool


class PCDesktopTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows launch flags')
    async def test_launch_creates_visible_console_in_desktop_without_command_or_secrets(self):
        process = MagicMock(pid=123); process.poll.return_value = None
        with patch('athena.pc_desktop.subprocess.Popen', return_value=process) as launch, \
             patch('athena.pc_desktop.asyncio.sleep', AsyncMock()), \
             patch('athena.tools.command.windows_desktop', return_value=Path.cwd()), \
             patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'do-not-inherit'}):
            result = await PCDesktop().execute({'action': 'open_terminal'})
        self.assertEqual(result['pid'], 123)
        args, kwargs = launch.call_args
        self.assertEqual(args[0][1:], ['-NoLogo', '-NoProfile', '-NoExit'])
        self.assertTrue(kwargs['creationflags'] & __import__('subprocess').CREATE_NEW_CONSOLE)
        self.assertNotIn('DEEPSEEK_API_KEY', kwargs['env'])

    async def test_extra_command_rejected_before_launch(self):
        with patch('athena.pc_desktop.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):
                await PCDesktop().execute({'action': 'open_terminal', 'command': 'whoami'})
        launch.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows launch flags')
    async def test_exited_terminal_does_not_produce_success(self):
        process = MagicMock(); process.poll.return_value = 1
        with patch('athena.pc_desktop.subprocess.Popen', return_value=process), \
             patch('athena.pc_desktop.asyncio.sleep', AsyncMock()):
            with self.assertRaisesRegex(ValueError, 'exited'):
                await PCDesktop().execute({'action': 'open_terminal'})

    async def test_pi_routes_terminal_to_pc_not_local_shell(self):
        with patch('athena.tools.pc_desktop.os.name', 'posix'), \
             patch('athena.tools.pc_desktop.pc_request', AsyncMock(return_value={'launched': True, 'pid': 123})) as remote:
            result = await PCDesktopTool().execute({'action': 'open_terminal'})
        self.assertTrue(result.success)
        remote.assert_awaited_once_with('/desktop', {'action': 'open_terminal'})

    async def test_remote_openclaw_discovered_without_pc_runtime_on_pi(self):
        with patch('athena.tools.openclaw.command', side_effect=FileNotFoundError), \
             patch.dict(os.environ, {'ATHENA_PC_UPLOAD_URL': 'http://192.168.1.2:8781/upload',
                                     'ATHENA_PC_TRANSFER_KEY': 'k'*64}):
            tools = create_tools()
        self.assertTrue(tools[0].remote)

    async def test_configured_pc_tools_not_silently_hidden_when_key_is_missing(self):
        with patch('athena.tools.openclaw.command', side_effect=FileNotFoundError), \
             patch('athena.tools.openclaw.load_local_environment'), \
             patch.dict(os.environ, {'ATHENA_PC_UPLOAD_URL': 'http://192.168.1.2:8781/upload',
                                     'ATHENA_PC_TRANSFER_KEY': ''}):
            names = [tool.definition.name for tool in create_tools()]
        self.assertEqual(names, ['openclaw_task', 'openclaw_status'])

    async def test_remote_openclaw_preserves_failure_receipt(self):
        with patch('athena.tools.openclaw.pc_request', AsyncMock(return_value={
                'success': False, 'spoken_text': 'Not completed', 'data': {'session_key': 'task-1'}})) as remote:
            result = await OpenClawTaskTool(remote=True).execute({'objective': 'Do a task'})
        self.assertFalse(result.success)
        self.assertEqual(result.data['session_key'], 'task-1')
        remote.assert_awaited_once()

    async def test_status_timeout_returns_speakable_error_without_restart(self):
        from athena.tools.openclaw import OpenClawStatusTool
        with patch('athena.tools.openclaw.pc_request', AsyncMock(side_effect=TimeoutError)) as remote:
            result = await OpenClawStatusTool(remote=True).execute({'action': 'status'})
        self.assertFalse(result.success)
        self.assertIn('no task was restarted', result.spoken_text)
        remote.assert_awaited_once_with('/openclaw', {'action': 'status'}, timeout=8)

    async def test_bridge_timeout_also_bounds_address_discovery(self):
        from athena.pc_bridge import pc_request
        async def stalled(*args):
            await asyncio.Event().wait()
        with patch.dict(os.environ, {'ATHENA_PC_UPLOAD_URL': 'http://192.168.1.2:8781/upload',
                                     'ATHENA_PC_TRANSFER_KEY': 'k'*64}), \
             patch('athena.pc_bridge.resolve_pc_url', side_effect=stalled):
            with self.assertRaises(TimeoutError):
                await pc_request('/openclaw', {'action': 'status'}, timeout=.02)

    async def test_signed_endpoints_prevent_replay_and_cross_endpoint_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            key = 'test'*16
            desktop = MagicMock(); desktop.execute = AsyncMock(return_value={'launched': True, 'pid': 123})
            agent = OpenClawTaskTool(); agent.execute = AsyncMock(return_value=ToolResult(True, 'Reply', {'session_key': '1'}))
            async with TestClient(TestServer(inbox_app(directory, key, desktop=desktop, openclaw=agent))) as client:
                for endpoint, body in [('/desktop', {'action': 'open_terminal'}), ('/openclaw', {'objective': 'Task'})]:
                    raw = json.dumps(body).encode()
                    stamp, nonce, digest = str(int(time.time())), uuid4().hex, hashlib.sha256(raw).hexdigest()
                    headers = {'X-Athena-Time': stamp, 'X-Athena-Nonce': nonce, 'X-Athena-Name': 'action.json',
                        'X-Athena-SHA256': digest,
                        'X-Athena-Signature': signature(key, stamp, nonce, 'POST:/browser:action.json', digest)}
                    self.assertEqual((await client.post(endpoint, data=raw, headers=headers)).status, 403)
                    headers['X-Athena-Signature'] = signature(key, stamp, nonce, f'POST:{endpoint}:action.json', digest)
                    response = await client.post(endpoint, data=raw, headers=headers)
                    self.assertEqual(response.status, 200)
                    self.assertEqual((await client.post(endpoint, data=raw, headers=headers)).status, 403)
                desktop.execute.assert_awaited_once()
                agent.execute.assert_awaited_once()
