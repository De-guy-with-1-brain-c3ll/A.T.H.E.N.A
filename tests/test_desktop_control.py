import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from athena.metrics import progress, read_progress, record, snapshot
from athena.web import health, reboot_pi, monitor_status


class MetricsTests(unittest.TestCase):
    def test_counters_aggregate_across_calls_without_overstating_provider_usage(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'ATHENA_DATA_DIR': root}):
            record('deepseek', {'requests': 1, 'estimated_input_tokens': 200})
            record('deepseek_reported', {'prompt_tokens': 180})
            record('qwen_stt', {'audio_seconds': 2.5, 'token_estimate': 'unknown'})
            record('deepseek', {'estimated_output_tokens': 20})
            result = snapshot()
            self.assertEqual(result['totals']['deepseek']['requests'], 1)
            self.assertEqual(result['totals']['deepseek_reported']['prompt_tokens'], 180)
            self.assertEqual(result['totals']['qwen_stt']['audio_seconds'], 2.5)
            self.assertNotIn('token_estimate', result['totals']['qwen_stt'])

    def test_progress_persists_and_corrupt_file_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'ATHENA_DATA_DIR': root}):
            progress('transfer', {'state': 'sending', 'bytes_done': 12, 'bytes_total': 20})
            self.assertEqual(read_progress('transfer')['bytes_done'], 12)
            (Path(root) / 'transfer-progress.json').write_text('broken')
            self.assertEqual(read_progress('transfer'), {})
            self.assertEqual(read_progress('download'), {})


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_pc_transfer_requires_verified_receipt_and_reports_bytes(self):
        from aiohttp.test_utils import TestClient, TestServer
        from athena.pc_transfer import inbox_app
        from athena.tools.pc_transfer import UploadTool
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); (root / 'reports').mkdir()
            artifact = root / 'reports/sample.txt'; artifact.write_bytes(b'ATHENA' * 20000)
            async with TestClient(TestServer(inbox_app(root / 'inbox', 'test' * 16))) as receiver:
                with patch.dict(os.environ, {'ATHENA_DATA_DIR': str(root), 'ATHENA_PC_TRANSFER_KEY': 'test' * 16,
                                           'ATHENA_PC_UPLOAD_URL': str(receiver.make_url('/upload'))}):
                    tool = UploadTool(); arguments, _ = await tool.prepare({'path': str(artifact)})
                    result = await tool.execute(arguments)
                    self.assertTrue(result.success)
                    sample = read_progress('transfer')
                    self.assertEqual(sample['state'], 'complete')
                    self.assertEqual(sample['bytes_done'], 120000)
                    self.assertEqual(sample['receipt']['bytes'], 120000)
                    self.assertEqual((root / 'inbox' / sample['receipt']['filename']).read_bytes(), artifact.read_bytes())

    async def test_health_identifies_athena_without_leaking_secrets(self):
        response = await health(None)
        self.assertEqual(json.loads(response.text), {'ok': True, 'app': 'athena', 'monitor_schema': 1})

    async def test_reboot_requires_auth_csrf_and_explicit_confirmation(self):
        from aiohttp import web
        request = MagicMock(); command = AsyncMock()
        with patch('athena.web._require_post'), patch('athena.web._json_body', AsyncMock(return_value={})), patch('athena.web._systemctl', command):
            with self.assertRaises(web.HTTPBadRequest): await reboot_pi(request)
            command.assert_not_awaited()
        with patch('athena.web._require_post', side_effect=web.HTTPUnauthorized), patch('athena.web._systemctl', command):
            with self.assertRaises(web.HTTPUnauthorized): await reboot_pi(request)
            command.assert_not_awaited()
        with patch('athena.web._require_post'), patch('athena.web._json_body', AsyncMock(return_value={'confirmation': 'REBOOT PI'})), patch('athena.web._systemctl', command):
            response = await reboot_pi(request)
            self.assertTrue(json.loads(response.text)['ok'])
            command.assert_awaited_once_with('reboot', '--no-block')

    async def test_monitor_reads_receipts_without_model_or_audio_calls(self):
        state = MagicMock(); registry = state.session.registry
        registry.status_store.rows.return_value = [{'state': 'running', 'tool': 'web_search'}]
        registry.get.return_value.manager.rows.return_value = []
        with patch('athena.web._require_auth', return_value=state), patch('athena.web.snapshot', return_value={'totals': {}}), patch('athena.web.read_progress', return_value={}), patch('athena.web.read_status', return_value={}):
            response = await monitor_status(MagicMock())
        self.assertEqual(json.loads(response.text)['operations'][0]['state'], 'running')
        registry.execute.assert_not_called()
        state.model.assert_not_called()


try:
    from athena.desktop import Client, PinnedConnection, discover, private_host
except ImportError:
    Client = None  # Minimal Pi images need no Tkinter to serve the desktop client.


@unittest.skipIf(Client is None, 'Desktop GUI runtime is Windows-only')
class DesktopClientTests(unittest.TestCase):
    def test_scan_is_limited_to_private_lan(self):
        for cidr in ('8.8.8.0/24', '192.168.0.0/16', '127.0.0.0/24', '::1/128'):
            with self.assertRaises(ValueError): discover(cidr)
        self.assertTrue(private_host('192.168.33.153'))
        self.assertFalse(private_host('127.0.0.1'))

    def test_certificate_mismatch_stops_connection_before_credentials(self):
        connection = PinnedConnection('192.168.33.153', 'wrong')
        connection.sock = MagicMock()
        connection.sock.getpeercert.return_value = b'certificate'
        with patch('http.client.HTTPSConnection.connect'), patch.object(connection, 'close') as close:
            import ssl
            with self.assertRaises(ssl.SSLError): connection.connect()
            close.assert_called_once()

    def test_login_without_password_obtains_cookie_then_csrf(self):
        client = Client('192.168.33.153', 'hash')
        with patch.object(client, 'request', side_effect=[{}, {'csrf': 'test'}]) as request:
            self.assertEqual(client.login(''), {'csrf': 'test'})
            self.assertEqual([c.args[0] for c in request.call_args_list], ['/', '/api/bootstrap'])

    def test_wrong_host_cannot_receive_credentials(self):
        for host in ('8.8.8.8', 'localhost', 'https://192.168.33.153:8780'):
            with self.assertRaises(ValueError): PinnedConnection(host)
