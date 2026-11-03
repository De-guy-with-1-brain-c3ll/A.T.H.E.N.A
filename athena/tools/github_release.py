"""Resolve an asset from an official public GitHub release; never download it."""
from __future__ import annotations

from urllib.parse import quote, unquote, urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from athena.tools._http import PublicHTTP, validate_url
from athena.tools.models import ToolDefinition, ToolResult


class GitHubReleaseAssetTool:
    definition = ToolDefinition(
        name="find_github_release_asset",
        description=("Find an exact downloadable asset in a public repository's latest GitHub release. "
                     "Use before download_file when the requested software is hosted in GitHub Releases. "
                     "Returns a stable official browser_download_url but never saves the file."),
        parameters={"type": "object", "properties": {
            "repository": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"},
            "filename_contains": {"type": "string", "minLength": 2, "maxLength": 100},
        }, "required": ["repository", "filename_contains"], "additionalProperties": False},
        timeout_seconds=20)

    def __init__(self, http=None):
        self.http = http or PublicHTTP(max_bytes=1_000_000, timeout=15)

    async def execute(self, arguments):
        repository = arguments["repository"]
        selector = arguments["filename_contains"].casefold()
        owner, repo = repository.split("/", 1)
        url = f"https://github.com/{quote(owner)}/{quote(repo)}/releases/latest"
        try:
            response = await self.http.get(url)
            soup = BeautifulSoup(response.text(), "html.parser")
            # GitHub's release page lazy-loads assets through an official HTML
            # fragment. Fetch that exact public fragment rather than guessing URLs.
            fragment = soup.find("include-fragment", src=lambda value: value and "/expanded_assets/" in value)
            if fragment is not None:
                fragment_url = urljoin(response.url, fragment["src"])
                fragment_response = await self.http.get(fragment_url)
                soup = BeautifulSoup(fragment_response.text(), "html.parser")
            candidates = []
            for anchor in soup.find_all("a", href=True):
                target = urljoin(response.url, anchor["href"])
                path = unquote(urlsplit(target).path)
                name = path.rsplit("/", 1)[-1]
                if selector not in name.casefold():
                    continue
                validate_url(target)
                parsed = urlsplit(target)
                if parsed.scheme != "https" or parsed.hostname != "github.com":
                    continue
                if "/releases/download/" not in path:
                    continue
                item = {"filename": name, "url": target}
                if item not in candidates:
                    candidates.append(item)
            tag = None
            path_parts = urlsplit(response.url).path.split("/")
            if "tag" in path_parts:
                tag = path_parts[path_parts.index("tag") + 1]
            if not candidates:
                return ToolResult(False,
                    f"The latest {repository} release has no asset matching {arguments['filename_contains']}.",
                    {"repository": repository, "tag": tag, "matches": []})
            exact_suffix = [item for item in candidates
                            if item["filename"].casefold().endswith(selector)]
            if len(exact_suffix) == 1:
                candidates = exact_suffix
            if len(candidates) > 1:
                return ToolResult(False, "More than one release asset matched; use a more exact filename.",
                                  {"repository": repository, "tag": tag,
                                   "matches": candidates})
            chosen = candidates[0]
            return ToolResult(True, f"I found {chosen['filename']} in the official latest release.",
                              {"repository": repository, "tag": tag, **chosen,
                               "source": response.url, "untrusted_content": True})
        except (ValueError, aiohttp.ClientError, TimeoutError, KeyError, TypeError):
            return ToolResult(False, "The official GitHub release metadata could not be retrieved.",
                              {"repository": repository, "error": "release_lookup_failed"})


def create_tools():
    return [GitHubReleaseAssetTool()]
