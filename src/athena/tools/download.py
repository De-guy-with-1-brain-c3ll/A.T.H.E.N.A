"""Approval-gated downloads, never automatic opening or execution."""
from __future__ import annotations
import os
from pathlib import Path
import re
import tempfile
from email.message import Message
from urllib.parse import urlsplit
import aiohttp
from athena.tools._http import PublicHTTP, DownloadRedirect, validate_url
from athena.tools.coding import check_link
from athena.tools.models import PermissionLevel, ToolDefinition, ToolResult
from athena.paths import data_directory


class DownloadTool:
    definition = ToolDefinition(
        name="download_file",
        description="Prepare a requested public HTTPS file for approval: resolve redirects with HEAD requests, reject webpages, and show the real filename/site/size. Call this tool BEFORE asking for approval; never ask for approval in your own text. Saving the file starts only after the hub receives user approval. Maximum 1 GiB; no automatic execution. An imager application and an OS disk image are different files: clarify which the user wants if unsure.",
        parameters={"type": "object", "properties": {
            "url": {"type": "string", "pattern": "^https://", "maxLength": 4096},
            "filename": {"type": "string", "minLength": 1, "maxLength": 120},
        }, "required": ["url", "filename"], "additionalProperties": False},
        permission=PermissionLevel.CONFIRM, timeout_seconds=660)

    def __init__(self, root=None, http=None):
        self.root = Path(root) if root is not None else data_directory() / "downloads"
        self.http = http or PublicHTTP(max_bytes=1024 * 1024 * 1024, timeout=600)

    async def prepare(self, arguments):
        self.target(arguments)
        try:
            metadata = await self.http.inspect_download(arguments["url"])
            if metadata["content_type"] in {"text/html", "application/xhtml+xml"}:
                return ToolResult(False, "That link is a webpage, not the requested file. I need to find the actual download.",
                                  {"error": "webpage_not_file", "url": metadata["url"]})
            size = metadata.get("bytes")
            if size is not None and size > 1024 * 1024 * 1024:
                return ToolResult(False, "That file exceeds the 1 GiB download limit.", {"error": "file_too_large", **metadata})
            filename = arguments["filename"]
            header = Message()
            header["Content-Disposition"] = metadata.get("content_disposition", "")
            server_name = header.get_filename()
            if server_name:
                # Never trust a server-supplied path; only accept a valid plain filename.
                self.target({"url": metadata["url"], "filename": server_name})
                filename = server_name
            prepared = {"url": metadata["url"], "filename": filename}
            self.target(prepared)
            size_text = f"about {size / (1024*1024):.1f} MiB" if size is not None else "size unknown, up to 1 GiB"
            prompt = (f"Download {filename} from {urlsplit(metadata['url']).hostname}, {size_text}? "
                      "Say yes to approve, or no to cancel.")
            return ToolResult(True, prompt, {"arguments": prepared, "metadata": metadata})
        except (ValueError, OSError, aiohttp.ClientError, TimeoutError) as error:
            detail = str(error) if isinstance(error, ValueError) else "The download server could not be reached."
            return ToolResult(False, f"I couldn't prepare the download. {detail}", {"error": "preparation_failed"})

    def target(self, arguments):
        validate_url(arguments["url"])
        if urlsplit(arguments["url"]).scheme != "https":
            raise ValueError("Downloads require a public HTTPS URL.")
        name = arguments["filename"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,119}", name) or name.endswith((".", " ")):
            raise ValueError("Use a plain filename, not a path or hidden file.")
        reserved = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}
        if name.split(".")[0].upper() in reserved:
            raise ValueError("Reserved filename.")
        if Path(name).suffix.lower() in {".lnk", ".url", ".scf", ".desktop", ".library-ms", ".search-ms"}:
            raise ValueError("Shortcut and shell-integration files are not allowed.")
        for ancestor in (*self.root.parents, self.root, self.root / name):
            check_link(ancestor)
        target = self.root / name
        if target.exists():
            raise ValueError("That filename already exists. Choose a new name; existing files are never overwritten.")
        return target

    async def execute(self, arguments):
        return await self.execute_with_progress(arguments)

    async def execute_with_progress(self, arguments, progress=None):
        temporary = None
        try:
            target = self.target(arguments)
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".athena-", suffix=".part", dir=self.root, delete=False) as stream:
                temporary = Path(stream.name)
                data = await self.http.download(arguments["url"], stream, progress=progress)
            if os.name == "nt":
                # Preserve Windows' internet-file warning, without recording URL secrets.
                Path(str(temporary) + ":Zone.Identifier").write_text("[ZoneTransfer]\nZoneId=3\n", encoding="utf-8")
            self.target(arguments)  # Recheck links/existing file after network I/O.
            os.link(temporary, target)  # Atomic publication with no overwrite.
            return ToolResult(True, f"Downloaded {target.name}. I have not opened or run it.",
                              {**data, "path": str(target), "executed": False, "untrusted_content": True})
        except DownloadRedirect as error:
            return ToolResult(False, "The file moved to another download server. I need approval for the updated destination.",
                              {"redirect_url": error.url})
        except (ValueError, OSError, aiohttp.ClientError, TimeoutError) as error:
            detail = str(error) if isinstance(error, ValueError) else "Network or file storage error."
            return ToolResult(False, f"Download failed. {detail}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def create_tools():
    return [DownloadTool()]
