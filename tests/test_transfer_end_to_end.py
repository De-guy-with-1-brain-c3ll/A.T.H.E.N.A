"""Offline end-to-end checks for download, coding, and signed PC transfer."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from athena.metrics import read_progress
from athena.pc_transfer import inbox_app
from athena.pc_discovery import resolve_pc_url
from athena.tools.coding import CodingWorkspaceTool
from athena.tools.download import DownloadTool
from athena.tools.pc_transfer import UploadTool, TransferStatus


class TransferEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_inbox_discovery_after_address_change_checks_shared_key(self):
        import ipaddress
        from athena import pc_discovery
        key = 'test' * 16
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        server = TestServer(inbox_app(Path(temporary.name) / 'inbox', key),
                            host='127.0.0.1')
        await server.start_server()
        self.addAsyncCleanup(server.close)
        stale = f'http://127.0.0.2:{server.port}/upload'
        with patch.object(pc_discovery.ipaddress, 'ip_network',
                          return_value=ipaddress.IPv4Network('127.0.0.0/30')):
            self.assertEqual(await resolve_pc_url(stale, key),
                             f'http://127.0.0.1:{server.port}/upload')
            with self.assertRaises(ConnectionError):
                await resolve_pc_url(stale, 'wrong' * 13)

    async def test_status_reports_percentage_and_transferred_size_while_sending(self):
        sample = {'state': 'sending', 'bytes_done': 512, 'bytes_total': 2048,
                  'percent_complete': 25.0, 'transferred_size': '512 bytes'}
        with patch('athena.metrics.read_progress', return_value=sample):
            result = await TransferStatus(UploadTool()).execute({})
        self.assertIn('25.0%', result.spoken_text)
        self.assertIn('512 of 2048 bytes', result.spoken_text)
        self.assertEqual(result.data['transferred_size'], '512 bytes')

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'ATHENA_DATA_DIR': str(self.root),
            'ATHENA_PC_TRANSFER_KEY': 'test' * 16})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = TestClient(TestServer(inbox_app(self.root / 'inbox', 'test' * 16)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        os.environ['ATHENA_PC_UPLOAD_URL'] = str(self.client.make_url('/upload'))

    async def send(self, path, expected):
        tool = UploadTool()
        prepared, _ = await tool.prepare({'path': str(path)})
        result = await tool.execute(prepared)
        self.assertTrue(result.success, result.spoken_text)
        self.assertEqual(result.data['percent_complete'], 100)
        self.assertEqual(result.data['bytes'], len(expected))
        self.assertEqual(read_progress('transfer')['bytes_done'], len(expected))
        received = self.root / 'inbox' / result.data['filename']
        self.assertEqual(received.read_bytes(), expected)
        status = await TransferStatus(tool).execute({})
        self.assertEqual(status.data['percent_complete'], 100)

    async def test_downloaded_files_of_varied_sizes_reach_pc_intact(self):
        for length in (1, 65537, 1024 * 1024 + 17):
            with self.subTest(length=length):
                payload = bytes((i % 251 for i in range(length)))
                http = AsyncMock()
                async def download(_url, stream, progress=None):
                    for offset in range(0, len(payload), 8192):
                        stream.write(payload[offset:offset + 8192])
                    return {'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
                http.download.side_effect = download
                tool = DownloadTool(root=self.root / 'downloads', http=http)
                result = await tool.execute({'url': 'https://example.com/file.bin',
                                             'filename': f'file-{length}.bin'})
                self.assertTrue(result.success, result.spoken_text)
                await self.send(Path(result.data['path']), payload)

    async def test_coding_output_reaches_pc_and_changed_file_is_rejected(self):
        coding = CodingWorkspaceTool(root=self.root / 'coding')
        self.assertTrue((await coding.execute({'action': 'create', 'project': 'transfer'})).success)
        source = 'print("hello from ATHENA")\n'
        written = await coding.execute({'action': 'write', 'project': 'transfer',
                                        'path': 'main.py', 'content': source})
        self.assertTrue(written.success)
        tool = UploadTool(); tool.coding = coding
        prepared, _ = await tool.prepare({})
        Path(written.data['path']).write_text(source + '# changed\n')
        rejected = await tool.execute(prepared)
        self.assertFalse(rejected.success)
        prepared, _ = await tool.prepare({})
        result = await tool.execute(prepared)
        self.assertTrue(result.success, result.spoken_text)
        received = self.root / 'inbox' / result.data['filename']
        self.assertEqual(received.read_text(), source + '# changed\n')

    async def test_saved_file_survives_registry_restart_and_cross_interface_transfer(self):
        coding = CodingWorkspaceTool(root=self.root / 'coding')
        await coding.execute({'action': 'create', 'project': 'persist'})
        await coding.execute({'action': 'write', 'project': 'persist', 'path': 'main.py',
                              'content': 'print(42)\n'})
        new_upload = UploadTool()
        new_upload.coding = CodingWorkspaceTool(root=self.root / 'coding')
        prepared, _ = await new_upload.prepare({})
        result = await new_upload.execute(prepared)
        self.assertTrue(result.success, result.spoken_text)
        self.assertEqual((self.root / 'inbox' / result.data['filename']).read_text(), 'print(42)\n')
