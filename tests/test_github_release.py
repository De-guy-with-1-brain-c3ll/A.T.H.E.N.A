import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace as NS

from athena.tools.github_release import GitHubReleaseAssetTool
from athena.tools.download import DownloadTool
from athena.tools.registry import ToolRegistry


class GitHubReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.download_root = Path(self.temp.name) / "downloads"

    async def asyncTearDown(self):
        self.temp.cleanup()

    @staticmethod
    def page(assets):
        links = ''.join(f'<a href="/armbian/imager/releases/download/v2.0.4/{name}">{name}</a>'
                        for name in assets)
        return NS(url='https://github.com/armbian/imager/releases/tag/v2.0.4',
                  text=lambda: '<html><body>' + links + '</body></html>')

    @staticmethod
    def lazy_page(assets):
        main = NS(url='https://github.com/armbian/imager/releases/tag/v2.0.4',
            text=lambda: '<include-fragment src="/armbian/imager/releases/expanded_assets/v2.0.4"></include-fragment>')
        expanded = GitHubReleaseTests.page(assets)
        return [main, expanded]

    async def test_finds_one_official_asset(self):
        http = AsyncMock()
        http.get.return_value = self.page([
            'Armbian.Imager_2.0.4_x64-setup.exe', 'Armbian.Imager_2.0.4_aarch64.AppImage'])
        result = await GitHubReleaseAssetTool(http).execute({
            "repository": "armbian/imager", "filename_contains": "x64-setup.exe"})
        self.assertTrue(result.success)
        self.assertEqual(result.data['filename'], 'Armbian.Imager_2.0.4_x64-setup.exe')
        self.assertEqual(result.data['tag'], 'v2.0.4')

    async def test_follows_official_lazy_asset_fragment(self):
        http = AsyncMock()
        http.get.side_effect = self.lazy_page([
            'Armbian.Imager_2.0.4_x64-setup.exe',
            'Armbian.Imager_2.0.4_x64-setup.exe.sig'])
        result = await GitHubReleaseAssetTool(http).execute({
            "repository": "armbian/imager", "filename_contains": "x64-setup.exe"})
        self.assertTrue(result.success)
        self.assertEqual(http.get.await_count, 2)
        self.assertIn('/expanded_assets/v2.0.4', http.get.await_args_list[1].args[0])

    async def test_rejects_non_github_asset_url_and_ambiguous_match(self):
        http = AsyncMock()
        tool = GitHubReleaseAssetTool(http)
        http.get.return_value = NS(url='https://github.com/owner/repo/releases/tag/v1',
            text=lambda: '<a href="https://evil.example/releases/download/v1/tool.exe">tool</a>')
        self.assertFalse((await tool.execute({"repository": "owner/repo",
            "filename_contains": "tool"})).success)
        http.get.return_value = NS(url='https://github.com/owner/repo/releases/tag/v1', text=lambda:
            '<a href="/o/r/releases/download/v/tool-one.exe">one</a>'
            '<a href="/o/r/releases/download/v/tool-two.exe">two</a>')
        result = await tool.execute({"repository": "owner/repo", "filename_contains": "tool"})
        self.assertFalse(result.success)
        self.assertEqual(len(result.data['matches']), 2)

    async def test_armbian_command_is_host_routed_and_ambiguous_request_clarifies(self):
        release_http = AsyncMock()
        release_http.get.return_value = self.page(['Armbian.Imager_2.0.4_x64-setup.exe'])
        download_http = AsyncMock()
        download_http.inspect_download.return_value = {
            "url": "https://release-assets.githubusercontent.com/imager.exe", "bytes": 5_000_000,
            "content_type": "application/octet-stream", "content_disposition": ""}
        registry = ToolRegistry()
        registry.register(GitHubReleaseAssetTool(release_http))
        registry.register(DownloadTool(root=self.download_root, http=download_http))
        clarify = await registry.handle_user_command('attempt the armbian download')
        self.assertIn('Imager for Windows', clarify.spoken_text)
        self.assertFalse(registry.has_pending_download)
        prompt = await registry.handle_user_command('windows')
        self.assertTrue(prompt.data['approval_required'])
        self.assertTrue(registry.has_pending_download)
        release_http.get.assert_awaited_once()
        download_http.inspect_download.assert_awaited_once()

    async def test_failed_host_resolution_can_retry_without_model(self):
        release_http = AsyncMock()
        release_http.get.side_effect = [TimeoutError(), *self.lazy_page([
            'Armbian.Imager_2.0.4_x64-setup.exe'])]
        download_http = AsyncMock()
        download_http.inspect_download.return_value = {
            "url": "https://release-assets.githubusercontent.com/imager.exe", "bytes": 5,
            "content_type": "application/octet-stream", "content_disposition": ""}
        registry = ToolRegistry()
        registry.register(GitHubReleaseAssetTool(release_http))
        registry.register(DownloadTool(root=self.download_root, http=download_http))
        first = await registry.handle_user_command('download the armbian imager')
        self.assertFalse(first.success)
        self.assertEqual(registry._retry_action, 'armbian_imager')
        retried = await registry.handle_user_command('try again')
        self.assertTrue(retried.data['approval_required'])
        self.assertTrue(registry.has_pending_download)
        self.assertIsNone(registry._retry_action)

    async def test_inline_armbian_preapproval_still_requires_fresh_consent(self):
        release_http = AsyncMock()
        release_http.get.return_value = self.page(['Armbian.Imager_2.0.4_x64-setup.exe'])
        download_http = AsyncMock()
        download_http.inspect_download.return_value = {"url": "https://github.com/armbian/imager/releases/download/v2.0.4/imager.exe",
            "bytes": 5, "content_type": "application/octet-stream", "content_disposition": ""}
        registry = ToolRegistry()
        registry.register(GitHubReleaseAssetTool(release_http))
        registry.register(DownloadTool(root=self.download_root, http=download_http))
        prompt = await registry.handle_user_command('download the armbian imager, I approve')
        self.assertTrue(prompt.data['approval_required'])
        download_http.download.assert_not_called()
