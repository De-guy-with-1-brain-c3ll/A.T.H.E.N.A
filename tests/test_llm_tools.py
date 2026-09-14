import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock, patch
from uuid import uuid4

from athena.llm.deepseek import DeepSeekLanguageModel, DeepSeekUnavailable
from openai import APIConnectionError, APITimeoutError
from athena.settings.store import RuntimeSettingsStore
from athena.tools.coding import CodingWorkspaceTool
from athena.tools.download import DownloadTool
from athena.tools.command import CommandTool
from athena.tools.registry import ToolRegistry


def chunk(text=None, calls=None):
    return NS(choices=[NS(delta=NS(content=text, tool_calls=calls))])


def tool_stream(arguments):
    raw = json.dumps(arguments)
    # Exercise fragmented tool arguments as returned by a streaming API.
    return [chunk(calls=[NS(index=0, id='call_1', function=NS(
        name='coding_workspace', arguments=raw[:12]))]),
        chunk(calls=[NS(index=0, id=None, function=NS(
            name=None, arguments=raw[12:]))])]


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for item in self.chunks:
            yield item


class LLMToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_written_command_approval_is_replaced_by_real_tool_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry()
            registry.register(CommandTool(root, root / "desktop"))
            model = DeepSeekLanguageModel('test', 'test', registry,
                RuntimeSettingsStore(root / 'settings.json'))
            arguments = {"shell": "powershell", "cwd": "desktop",
                         "command": "New-Item -ItemType Directory -Path test"}
            tool = NS(index=0, id='real-command', function=NS(
                name='run_command', arguments=json.dumps(arguments)))
            fake = ('I will create it. The PowerShell command will be:\n' +
                    arguments["command"] + '\nSay yes to approve.')
            model._client.chat.completions.create = AsyncMock(side_effect=[
                Stream([chunk(fake)]), Stream([chunk(calls=[tool])])])
            try:
                answer = ''.join([part async for part in model.stream_reply(
                    uuid4(), 'create a folder on my desktop')])
                self.assertNotIn('I will create it', answer)
                self.assertIn('Run this powershell command', answer)
                self.assertTrue(registry.has_pending_approval)
                self.assertEqual(registry._pending[0], 'run_command')
            finally:
                await model.close()

    async def test_yes_without_host_approval_never_reaches_model(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry()
            model = DeepSeekLanguageModel('test', 'test', registry,
                RuntimeSettingsStore(Path(directory) / 'settings.json'))
            model._client.chat.completions.create = AsyncMock()
            try:
                answer = ''.join([part async for part in model.stream_reply(uuid4(), 'yes')])
                self.assertIn('no real action', answer.casefold())
                model._client.chat.completions.create.assert_not_awaited()
            finally:
                await model.close()

    async def test_fast_connection_failure_retries_once(self):
        with tempfile.TemporaryDirectory() as directory:
            model = DeepSeekLanguageModel('test', 'test', ToolRegistry(),
                RuntimeSettingsStore(Path(directory) / 'settings.json'))
            error = APIConnectionError(request=MagicMock())
            model._client.chat.completions.create = AsyncMock(side_effect=[
                error, Stream([chunk('Recovered.')])])
            try:
                with patch('athena.llm.deepseek.asyncio.sleep', new=AsyncMock()):
                    answer = ''.join([part async for part in model.stream_reply(uuid4(), 'hello')])
                self.assertEqual(answer, 'Recovered.')
                self.assertEqual(model._client.chat.completions.create.await_count, 2)
            finally:
                await model.close()

    async def test_timeout_becomes_safe_error_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            model = DeepSeekLanguageModel('test', 'test', ToolRegistry(),
                RuntimeSettingsStore(Path(directory) / 'settings.json'))
            model._client.chat.completions.create = AsyncMock(
                side_effect=APITimeoutError(request=MagicMock()))
            try:
                with self.assertRaisesRegex(DeepSeekUnavailable, 'too long'):
                    _ = [part async for part in model.stream_reply(uuid4(), 'hello')]
                self.assertEqual(model._client.chat.completions.create.await_count, 1)
            finally:
                await model.close()

    async def test_model_cannot_invent_download_progress_after_typo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry()
            registry._last_download = {"status": "prepared", "requested": {
                "filename": "safe.exe", "url": "https://example.com/safe.exe"}}
            model = DeepSeekLanguageModel('test', 'test', registry,
                RuntimeSettingsStore(root / 'settings.json'))
            model._client.chat.completions.create = AsyncMock(
                return_value=Stream([chunk('Downloading safe.exe now.')]))
            try:
                answer = ''.join([part async for part in model.stream_reply(uuid4(), 'unclear')])
                self.assertNotIn('Downloading', answer)
                self.assertIn('never started', answer)
            finally:
                await model.close()
    async def test_download_confirmation_is_host_controlled_and_silent_until_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            http = AsyncMock()
            http.inspect_download.return_value = {'url': 'https://example.com/test.txt', 'bytes': 5,
                'content_type': 'application/octet-stream', 'content_disposition': ''}
            async def download(url, stream, progress=None):
                stream.write(b'hello')
                return {'bytes': 5}
            http.download.side_effect = download
            registry = ToolRegistry()
            registry.register(DownloadTool(root / 'downloads', http))
            model = DeepSeekLanguageModel('test-key', 'test-model', registry,
                                          RuntimeSettingsStore(root / 'settings.json'))
            call = NS(index=0, id='download-1', function=NS(name='download_file',
                arguments=json.dumps({'url': 'https://example.com/test.txt', 'filename': 'test.txt'})))
            model._client.chat.completions.create = AsyncMock(return_value=Stream([
                chunk('I will reason about which tool to use.'), chunk(calls=[call])]))
            try:
                prompt = ''.join([p async for p in model.stream_reply(uuid4(), 'Download test.txt')])
                self.assertIn('Say yes', prompt)
                self.assertNotIn('reason', prompt)
                http.download.assert_not_called()
                result = ''.join([p async for p in model.stream_reply(uuid4(), 'approve download')])
                self.assertIn('Download started', result)
                await registry.wait_for_downloads()
                self.assertEqual((root / 'downloads/test.txt').read_bytes(), b'hello')
                self.assertEqual(model._client.chat.completions.create.await_count, 1)
            finally:
                await model.close()

    async def test_model_cannot_speak_fake_download_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            http = AsyncMock()
            http.inspect_download.return_value = {'url': 'https://cdn.example/file.zip', 'bytes': 20,
                'content_type': 'application/zip', 'content_disposition': ''}
            registry = ToolRegistry()
            registry.register(DownloadTool(root / 'downloads', http))
            model = DeepSeekLanguageModel('test', 'test-model', registry, RuntimeSettingsStore(root / 'settings.json'))
            tool = NS(index=0, id='call', function=NS(name='download_file', arguments=json.dumps(
                {'url': 'https://example.com/file.zip', 'filename': 'file.zip'})))
            model._client.chat.completions.create = AsyncMock(side_effect=[
                Stream([chunk('Do you approve me downloading the tool for you?')]),
                Stream([chunk(calls=[tool])])])
            try:
                answer = ''.join([p async for p in model.stream_reply(uuid4(), 'Download the tool')])
                self.assertNotIn('Do you approve me', answer)
                self.assertIn('cdn.example', answer)
                self.assertTrue(registry.has_pending_download)
                http.download.assert_not_called()
            finally:
                await model.close()

    async def test_long_fake_imager_prompt_is_replaced_by_real_tool_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            http = AsyncMock()
            http.inspect_download.return_value = {
                'url': 'https://release-assets.githubusercontent.com/imager.exe',
                'bytes': 5_138_022, 'content_type': 'application/octet-stream',
                'content_disposition': ''}
            registry = ToolRegistry()
            registry.register(DownloadTool(root / 'downloads', http))
            model = DeepSeekLanguageModel('test', 'test', registry,
                RuntimeSettingsStore(root / 'settings.json'))
            arguments = {'url': 'https://github.com/armbian/imager.exe',
                         'filename': 'Armbian.Imager_2.0.4_x64-setup.exe'}
            tool = NS(index=0, id='real-grant', function=NS(
                name='download_file', arguments=json.dumps(arguments)))
            fake = ('Download Armbian.Imager_2.0.4_x64-setup.exe from '
                    'release-assets.githubusercontent.com, about 4.9 MiB? '
                    'Say yes to approve, or no to cancel.')
            model._client.chat.completions.create = AsyncMock(side_effect=[
                Stream([chunk(fake)]), Stream([chunk(calls=[tool])])])
            try:
                answer = ''.join([part async for part in model.stream_reply(
                    uuid4(), 'try again')])
                self.assertIn('Say yes', answer)
                self.assertTrue(registry.has_pending_download)
                self.assertEqual(model._client.chat.completions.create.await_count, 2)
                http.download.assert_not_called()
            finally:
                await model.close()

    async def test_premature_download_claim_is_repaired_into_tool_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            http = AsyncMock()
            http.inspect_download.return_value = {'url': 'https://example.com/imager.exe',
                'bytes': 10, 'content_type': 'application/octet-stream', 'content_disposition': ''}
            registry = ToolRegistry()
            registry.register(DownloadTool(root / 'downloads', http))
            model = DeepSeekLanguageModel('test', 'test', registry,
                RuntimeSettingsStore(root / 'settings.json'))
            tool = NS(index=0, id='grant', function=NS(name='download_file', arguments=json.dumps(
                {'url': 'https://example.com/imager.exe', 'filename': 'imager.exe'})))
            model._client.chat.completions.create = AsyncMock(side_effect=[
                Stream([chunk('Downloading the imager now.')]), Stream([chunk(calls=[tool])])])
            try:
                answer = ''.join([part async for part in model.stream_reply(
                    uuid4(), 'download the imager. i approve')])
                self.assertNotIn('Downloading', answer)
                self.assertTrue(registry.has_pending_download)
                http.download.assert_not_called()
            finally:
                await model.close()

    async def test_search_does_not_justify_false_github_unreachable_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry()
            registry.names = MagicMock(return_value=(
                'search_web', 'read_webpage', 'browse_webpage'))
            registry.definitions = MagicMock(return_value=[
                {"type": "function", "function": {"name": "search_web",
                    "description": "search", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "read_webpage",
                    "description": "read", "parameters": {"type": "object"}}},
            ])
            async def execute(name, arguments):
                if name == 'read_webpage':
                    return NS(success=True, spoken_text='read', data={'text': 'GitHub release'})
                return NS(success=True, spoken_text='found', data={'results': []})
            registry.execute = AsyncMock(side_effect=execute)
            model = DeepSeekLanguageModel('test', 'test', registry,
                RuntimeSettingsStore(root / 'settings.json'))
            search_call = NS(index=0, id='search', function=NS(name='search_web',
                arguments=json.dumps({'query': 'Armbian'})))
            read_call = NS(index=0, id='read', function=NS(name='read_webpage',
                arguments=json.dumps({'url': 'https://github.com/armbian/imager/releases'})))
            model._client.chat.completions.create = AsyncMock(side_effect=[
                Stream([chunk(calls=[search_call])]),
                Stream([chunk("I can't reach GitHub from my environment.")]),
                Stream([chunk(calls=[read_call])]),
                Stream([chunk('GitHub is reachable.')])])
            try:
                answer = ''.join([part async for part in model.stream_reply(
                    uuid4(), 'You can reach GitHub')])
                self.assertEqual(answer, 'GitHub is reachable.')
                self.assertNotIn("can't reach", answer)
                self.assertEqual(model._client.chat.completions.create.await_count, 4)
                self.assertEqual(registry.execute.await_args_list[-1].args[0], 'read_webpage')
            finally:
                await model.close()

    async def test_streamed_create_write_test_and_final_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry()
            runner = AsyncMock(return_value={'success': True, 'exit_code': 0, 'stdout': '1 test passed'})
            registry.register(CodingWorkspaceTool(root / 'coding', runner))
            model = DeepSeekLanguageModel('test-not-a-real-key', 'test-model', registry,
                                          RuntimeSettingsStore(root / 'settings.json'))
            requests = []
            responses = [
                [chunk('Let me plan and think about this.'), *tool_stream({'action': 'create', 'project': 'demo'})],
                tool_stream({'action': 'write', 'project': 'demo', 'path': 'main.py', 'content': 'print(42)\n'}),
                tool_stream({'action': 'test', 'project': 'demo'}),
                [NS(choices=[]), chunk('<think>Private analysis</think>Saved and tested.')],
            ]
            async def create(**kwargs):
                requests.append(json.loads(json.dumps(kwargs)))
                return Stream(responses.pop(0))
            model._client.chat.completions.create = AsyncMock(side_effect=create)
            try:
                answer = ''.join([text async for text in model.stream_reply(uuid4(), 'Write and test a program')])
            finally:
                await model.close()
            self.assertEqual(answer, 'Saved and tested.')
            self.assertEqual((root / 'coding/demo/main.py').read_text(), 'print(42)\n')
            self.assertEqual(runner.call_args.args[1], 'test')
            results = [json.loads(m['content']) for m in requests[-1]['messages'] if m['role'] == 'tool']
            self.assertEqual(len(results), 3)
            self.assertTrue(all(r['success'] for r in results))
            self.assertGreaterEqual(requests[0]['max_tokens'], 4096)
