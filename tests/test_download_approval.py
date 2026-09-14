import asyncio
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from athena.tools.download import DownloadTool
from athena.tools.registry import ToolRegistry
from athena.tools._http import DownloadRedirect, PublicHTTP, PublicWebError


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'downloads'
        self.http = AsyncMock()
        self.http.inspect_download.return_value = {'url': 'https://example.com/sample.txt',
            'bytes': 12, 'content_type': 'application/octet-stream', 'content_disposition': ''}
        async def download(url, stream, progress=None):
            stream.write(b'example file')
            if progress:
                progress(12, 12)
            return {'bytes': 12, 'url': url, 'sha256': 'test-checksum'}
        self.http.download.side_effect = download
        self.tool = DownloadTool(self.root, self.http)
        self.registry = ToolRegistry()
        self.registry.register(self.tool)
        self.arguments = {'url': 'https://example.com/sample.txt', 'filename': 'sample.txt'}

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_metadata_only_before_explicit_approval_and_one_use(self):
        result = await self.registry.execute('download_file', self.arguments)
        self.assertTrue(result.data['approval_required'])
        self.assertIn('sample.txt from example.com', result.spoken_text)
        self.http.download.assert_not_called()
        self.http.inspect_download.assert_awaited_once()
        self.assertFalse(self.root.exists())
        vague = await self.registry.handle_user_command('okay')
        self.assertIn('Say yes', vague.spoken_text)
        self.http.download.assert_not_called()
        result = await self.registry.handle_user_command('Yes，I approve。')
        self.assertTrue(result.success, result.spoken_text)
        await self.registry.wait_for_downloads()
        self.assertEqual((self.root / 'sample.txt').read_bytes(), b'example file')
        self.assertFalse(self.registry.download_status().data['result']['executed'])
        self.assertFalse(list(self.root.glob('*.part')))
        if os.name == 'nt':
            self.assertIn('ZoneId=3', Path(str(self.root / 'sample.txt') + ':Zone.Identifier').read_text())
        self.assertFalse((await self.registry.handle_user_command('approve download')).success)
        self.assertEqual(self.http.download.await_count, 1)

    async def test_approval_bound_to_snapshot_not_mutable_arguments(self):
        await self.registry.execute('download_file', self.arguments)
        self.arguments['url'] = 'https://other.example/evil.exe'
        self.arguments['filename'] = 'evil.exe'
        await self.registry.handle_user_command('approve download')
        await self.registry.wait_for_downloads()
        self.assertEqual(self.http.download.call_args.args[0], 'https://example.com/sample.txt')
        self.assertFalse((self.root / 'evil.exe').exists())

    async def test_yes_and_approve_only_authorize_pending_request(self):
        for phrase in ['yes', 'ys', 'y', 'yep', 'yeah', 'sure', 'do it', 'proceed',
                       'approve', 'yes please', 'Yes，I approve。']:
            await self.registry.execute('download_file', self.arguments)
            self.assertTrue(self.registry.is_confirmation_reply(phrase))
            result = await self.registry.handle_user_command(phrase)
            self.assertTrue(result.success, result.spoken_text)
            await self.registry.wait_for_downloads()
            (self.root / 'sample.txt').unlink()
        for phrase in ['yes系', 'yes but not now', 'yes do not download it']:
            await self.registry.execute('download_file', self.arguments)
            self.assertFalse(self.registry.is_confirmation_reply(phrase))
            await self.registry.handle_user_command(phrase)
            self.assertFalse(self.registry.has_pending_download)

    async def test_resolves_destination_and_298_mib_file_before_asking(self):
        self.http.inspect_download.return_value = {'url': 'https://cdn.example/image.img.xz',
            'bytes': 312974988, 'content_type': 'application/octet-stream',
            'content_disposition': 'attachment; filename=image.img.xz'}
        result = await self.registry.execute('download_file', self.arguments)
        self.assertIn('cdn.example', result.spoken_text)
        self.assertIn('298.5 MiB', result.spoken_text)
        self.assertEqual(result.data['filename'], 'image.img.xz')
        self.http.download.assert_not_called()
        details = await self.registry.handle_user_command('OK, GIVE ME THE URL RIGHT NOW.')
        self.assertEqual(details.data['display_url'], 'https://cdn.example/image.img.xz')
        self.assertTrue(self.registry.has_pending_download)
        self.assertTrue((await self.registry.handle_user_command('yes')).success)
        await self.registry.wait_for_downloads()
        self.assertEqual(self.http.download.call_args.args[0], 'https://cdn.example/image.img.xz')

    async def test_webpage_and_oversized_files_never_create_approval(self):
        for content_type, size in [('text/html', 123), ('application/octet-stream', 1024**3 + 1)]:
            self.http.inspect_download.return_value.update(content_type=content_type, bytes=size)
            result = await self.registry.execute('download_file', self.arguments)
            self.assertFalse(result.success)
            self.assertFalse(result.data.get('approval_required', False))
            self.assertFalse(self.registry.has_pending_download)
        self.http.download.assert_not_called()
        self.assertFalse(self.root.exists())

    async def test_expiration_cancellation_and_new_command_invalidate(self):
        for command in ['cancel download', 'what time is it']:
            await self.registry.execute('download_file', self.arguments)
            await self.registry.handle_user_command(command)
            self.assertFalse((await self.registry.handle_user_command('approve download')).success)
        with patch('athena.tools.registry.time.monotonic', return_value=100):
            await self.registry.execute('download_file', self.arguments)
        with patch('athena.tools.registry.time.monotonic', return_value=221):
            result = await self.registry.handle_user_command('approve download')
        self.assertIn('expired', result.spoken_text)
        self.http.download.assert_not_called()

    async def test_injected_approval_argument_and_unsafe_paths_rejected(self):
        with self.assertRaises(ValueError):
            await self.registry.execute('download_file', {**self.arguments, 'confirmed': True})
        for name in ['../escape.exe', '.env', 'NUL.txt', 'file.txt:stream', 'evil.lnk', 'file.', 'C:/a.txt']:
            with self.assertRaises(ValueError, msg=name):
                await self.registry.execute('download_file', {**self.arguments, 'filename': name})
        for url in ['https://127.0.0.1/a', 'http://example.com/a', 'https://user:pass@example.com/a']:
            with self.assertRaises(ValueError):
                await self.registry.execute('download_file', {**self.arguments, 'url': url})
        self.http.download.assert_not_called()

    async def test_no_overwrite_even_if_file_appears_after_approval(self):
        await self.registry.execute('download_file', self.arguments)
        self.root.mkdir()
        target = self.root / 'sample.txt'
        target.write_text('existing')
        result = await self.registry.handle_user_command('approve download')
        self.assertTrue(result.success)
        await self.registry.wait_for_downloads()
        self.assertEqual(self.registry.download_status().data['status'], 'failed')
        self.assertEqual(target.read_text(), 'existing')
        self.http.download.assert_not_called()

    async def test_failure_and_cancellation_remove_partial_file(self):
        async def fail(url, stream, progress=None):
            stream.write(b'partial')
            raise PublicWebError('interrupted')
        self.http.download.side_effect = fail
        await self.registry.execute('download_file', self.arguments)
        self.assertTrue((await self.registry.handle_user_command('approve download')).success)
        await self.registry.wait_for_downloads()
        self.assertEqual(self.registry.download_status().data['status'], 'failed')

    async def test_cross_server_redirect_pauses_for_fresh_visible_approval(self):
        self.http.download.side_effect = DownloadRedirect('https://cdn.example/sample.txt')
        await self.registry.execute('download_file', self.arguments)
        await self.registry.handle_user_command('yes')
        await self.registry.wait_for_downloads()
        status = self.registry.download_status()
        self.assertEqual(status.data['status'], 'redirected')
        self.assertIn('paused', status.spoken_text)
        self.assertFalse(self.registry.has_pending_approval)

        self.http.inspect_download.return_value = {
            'url': 'https://cdn.example/sample.txt', 'bytes': 12,
            'content_type': 'application/octet-stream', 'content_disposition': ''}
        self.http.download.side_effect = None
        prompt = await self.registry.handle_user_command('continue the download')
        self.assertTrue(prompt.data['approval_required'])
        self.assertIn('cdn.example', prompt.spoken_text)
        self.assertTrue(self.registry.has_pending_approval)

    async def test_status_is_hub_generated_and_tracks_real_transfer(self):
        empty = await self.registry.handle_user_command('is the download in progress')
        self.assertEqual(empty.data['status'], 'none')
        await self.registry.execute('download_file', self.arguments)
        waiting = await self.registry.handle_user_command('is the download complete')
        self.assertEqual(waiting.data['status'], 'prepared')
        self.assertIn('not started', waiting.spoken_text)
        self.assertTrue(self.registry.has_pending_download)  # Status does not revoke consent.

        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(url, stream, progress=None):
            entered.set()
            if progress:
                progress(6, 12)
            await release.wait()
            stream.write(b'example file')
            return {'bytes': 12, 'url': url, 'sha256': 'checksum'}
        self.http.download.side_effect = slow
        transfer = await self.registry.handle_user_command('yes')
        await entered.wait()
        active = await self.registry.handle_user_command('download progress')
        self.assertEqual(active.data['status'], 'downloading')
        self.assertIn('50.0%', active.spoken_text)
        natural = await self.registry.handle_user_command("how much is downloaded and what's the speed")
        self.assertEqual(natural.data['status'], 'downloading')
        self.assertIn('per second', natural.spoken_text)
        release.set()
        await self.registry.wait_for_downloads()
        complete = await self.registry.handle_user_command('is download done')
        self.assertEqual(complete.data['status'], 'complete')
        self.assertIn('successfully', complete.spoken_text)

    async def test_natural_status_phrases_bypass_model_without_revoking_approval(self):
        await self.registry.execute('download_file', self.arguments)
        for phrase in ['is it downloading', 'is the download in progress',
                       'has it downloaded', 'is download complete']:
            result = await self.registry.handle_user_command(phrase)
            self.assertEqual(result.data['status'], 'prepared')
            self.assertTrue(self.registry.has_pending_download)

    async def test_failed_and_cancelled_transfer_status_are_not_model_guesses(self):
        async def fail(url, stream, progress=None):
            raise PublicWebError('failed')
        self.http.download.side_effect = fail
        await self.registry.execute('download_file', self.arguments)
        await self.registry.handle_user_command('yes')
        await self.registry.wait_for_downloads()
        self.assertEqual((await self.registry.handle_user_command('download status')).data['status'], 'failed')
        async def cancel(url, stream, progress=None):
            raise asyncio.CancelledError
        self.http.download.side_effect = cancel
        await self.registry.execute('download_file', self.arguments)
        await self.registry.handle_user_command('yes')
        await self.registry.wait_for_downloads()
        self.assertEqual((await self.registry.handle_user_command('download status')).data['status'], 'cancelled')
        self.assertEqual(list(self.root.iterdir()), [])
        async def cancel(url, stream, progress=None):
            stream.write(b'partial')
            raise asyncio.CancelledError
        self.http.download.side_effect = cancel
        await self.registry.execute('download_file', self.arguments)
        await self.registry.handle_user_command('approve download')
        await self.registry.wait_for_downloads()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse((await self.registry.handle_user_command('approve download')).success)


class Response:
    def __init__(self, status=200, headers=None, chunks=(), length=None):
        self.status, self.headers = status, headers or {}
        self.content_length, self.content_type = length, 'text/plain'
        self.url = 'https://example.com/a.txt'
        self.chunks = chunks
        self.content = self

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def iter_chunked(self, size):
        for chunk in self.chunks: yield chunk


class Session:
    def __init__(self, responses): self.responses, self.urls = responses, []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, url, **kwargs):
        self.urls.append(url)
        response = self.responses.pop(0)
        response.url = url
        return response

    def head(self, url, **kwargs):
        return self.get(url, **kwargs)


class DownloadTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_head_preparation_follows_public_https_cdn_without_body(self):
        session = Session([Response(302, {'Location': 'https://cdn.example/image.img.xz'}), Response(length=312974988)])
        with patch('athena.tools._http.aiohttp.TCPConnector'), patch('athena.tools._http.aiohttp.ClientSession', return_value=session):
            result = await PublicHTTP().inspect_download('https://example.com/download')
        self.assertEqual(result['url'], 'https://cdn.example/image.img.xz')
        self.assertEqual(result['bytes'], 312974988)
        self.assertEqual(len(session.urls), 2)

    async def test_head_preparation_rejects_private_and_downgrade_redirects(self):
        for target in ['https://127.0.0.1/file', 'http://public.example/file']:
            session = Session([Response(302, {'Location': target})])
            with patch('athena.tools._http.aiohttp.TCPConnector'), patch('athena.tools._http.aiohttp.ClientSession', return_value=session):
                with self.assertRaises(PublicWebError):
                    await PublicHTTP().inspect_download('https://example.com/download')
            self.assertEqual(len(session.urls), 1)

    async def fetch(self, responses, max_bytes=100):
        session = Session(responses)
        self.session = session
        with patch('athena.tools._http.aiohttp.TCPConnector'), patch('athena.tools._http.aiohttp.ClientSession', return_value=session):
            output = io.BytesIO()
            result = await PublicHTTP(max_bytes=max_bytes).download('https://example.com/a.txt', output)
            return result, output.getvalue()

    async def test_stream_checksum_and_same_origin_redirect(self):
        result, body = await self.fetch([Response(302, {'Location': '/b.txt'}), Response(chunks=[b'abc', b'def'])])
        self.assertEqual(body, b'abcdef')
        self.assertEqual(result['bytes'], 6)
        self.assertEqual(len(result['sha256']), 64)
        self.assertEqual(self.session.urls[-1], 'https://example.com/b.txt')

    async def test_cross_origin_and_private_redirects_not_contacted(self):
        for target in ['https://other.example/file', 'https://127.0.0.1/private', 'http://example.com/downgrade']:
            with self.assertRaises(PublicWebError):
                await self.fetch([Response(302, {'Location': target})])
            self.assertEqual(len(self.session.urls), 1)

    async def test_declared_and_streamed_size_limits(self):
        for response in [Response(length=101), Response(chunks=[b'a'*60, b'b'*50])]:
            with self.assertRaises(PublicWebError):
                await self.fetch([response])
