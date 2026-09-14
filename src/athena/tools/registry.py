from __future__ import annotations

import asyncio
import importlib
import pkgutil
import copy
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

from jsonschema import Draft202012Validator

from athena.tools.models import PermissionLevel, Tool, ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._pending: tuple[str, dict, float] | None = None
        self._shutdown_authorized = False
        self._control = {"shutdown_requested": False}
        self._last_download: dict = {}
        self._download_tasks: set[asyncio.Task] = set()
        self._last_command: dict = {}
        self._command_tasks: set[asyncio.Task] = set()
        self._armbian_choice_pending = False
        self._retry_action: str | None = None

    APPROVE = {"yes", "ys", "y", "ye", "yep", "yeah", "sure", "do it", "proceed",
               "yes please", "yes i approve", "i approve", "approve", "approve download",
               "yes approve download", "yes download it", "download it", "go ahead", "approved"}
    DENY = {"no", "no thanks", "no thank you", "cancel", "cancel download", "deny download"}

    @staticmethod
    def normalize_command(text):
        # Normalize punctuation but never discard non-English letters into a fake yes.
        text = unicodedata.normalize("NFKC", text).casefold()
        return " ".join(re.sub(r"[^\w\s]", " ", text).split())

    @property
    def has_pending_download(self):
        return (self._pending is not None and self._pending[0] == "download_file"
                and time.monotonic() < self._pending[2])

    @property
    def has_pending_approval(self):
        return self._pending is not None and time.monotonic() < self._pending[2]

    def is_confirmation_reply(self, text):
        return self.has_pending_approval and self.normalize_command(text) in self.APPROVE | self.DENY

    def download_context(self):
        return {"pending_approval": self.has_pending_download, "last_download": copy.deepcopy(self._last_download)}

    def command_context(self):
        # Never resend a potentially large command output to the model. Status
        # questions are answered locally by command_status().
        state = self._last_command
        compact = {key: copy.deepcopy(state[key]) for key in
                   ("status", "success", "started_at", "finished_at") if key in state}
        return {"pending_approval": self.has_pending_approval and self._pending[0] == "run_command",
                "last_command": compact}

    @property
    def shutdown_requested(self):
        return self._control["shutdown_requested"]

    def get(self, name: str):
        """Return a registered tool for lifecycle cleanup, if present."""
        return self._tools.get(name)

    @shutdown_requested.setter
    def shutdown_requested(self, value):
        self._control["shutdown_requested"] = bool(value)

    @classmethod
    def is_command_status_query(cls, text):
        command = cls.normalize_command(text)
        return bool(re.search(r"\b(command|cmd|powershell)\b", command) and
                    re.search(r"\b(status|progress|running|finished|done|failed|output)\b", command))

    def command_status(self):
        state = self._last_command
        if not state:
            return ToolResult(False, "I have no command recorded in this session.", {"status": "none"})
        status = state.get("status", "unknown")
        if status == "prepared":
            text = "That command is waiting for approval and has not run."
        elif status == "running":
            text = "The command is still running in the background."
        elif status in {"complete", "failed", "timed_out"}:
            result = state.get("result", {})
            text = state.get("message", f"The command {status}.")
            if result.get("stdout") and "Output:" not in text:
                text += "\nOutput:\n" + result["stdout"]
        elif status == "cancelled":
            text = "The command was cancelled."
        else:
            text = f"The recorded command status is {status}."
        return ToolResult(status == "complete", text, {"status": status, **copy.deepcopy(state)})

    @classmethod
    def is_download_status_query(cls, text):
        command = cls.normalize_command(text)
        download_word = re.search(r"\bdownload(?:s|ed|ing)?\b", command)
        status_word = re.search(
            r"\b(status|progress|complete|completed|finished|done|started|starting|saved|failed|working|speed|rate|percent|percentage|much)\b",
            command)
        pronoun_status = re.search(
            r"\b(?:is|was|has|did)\s+it\s+(?:still\s+)?(?:download(?:ing|ed)?|done|finished|failed|working)\b",
            command)
        return bool((download_word and status_word) or pronoun_status)

    def download_status(self):
        state = self._last_download
        if not state:
            return ToolResult(False, "I have no download recorded in this session.", {"status": "none"})
        status = state.get("status", "unknown")
        filename = (state.get("arguments") or state.get("requested") or {}).get("filename", "the file")
        if status == "prepared":
            if self.has_pending_download:
                text = f"{filename} is waiting for your approval; the download has not started."
            else:
                text = f"Approval for {filename} expired; the download never started."
        elif status == "downloading":
            received = int(state.get("bytes_downloaded", 0))
            total = state.get("bytes_total")
            speed = float(state.get("bytes_per_second", 0))
            received_text = self._human_bytes(received)
            speed_text = self._human_bytes(speed) + " per second" if speed else "calculating speed"
            if total:
                percent = min(100, received * 100 / total)
                text = (f"{filename} is {percent:.1f}% downloaded: {received_text} of "
                        f"{self._human_bytes(total)}, at {speed_text}.")
            else:
                text = f"{filename} has downloaded {received_text}, at {speed_text}."
        elif status == "complete":
            path = state.get("result", {}).get("path")
            if path and not Path(path).is_file():
                text = f"{filename} finished earlier, but the saved file is no longer present."
                status = "missing"
            else:
                text = f"{filename} downloaded successfully."
        elif status == "preparation_failed":
            text = f"{filename} never started because preparation failed."
        elif status == "cancelled":
            text = f"The download of {filename} was cancelled."
        elif status == "failed":
            text = state.get("message") or f"The download of {filename} failed."
        elif status == "redirected":
            text = state.get("message") or (
                f"The download of {filename} paused because the destination changed. "
                "Ask me to continue it so I can inspect the new server and request fresh approval."
            )
        else:
            text = f"The recorded status for {filename} is {status}."
        return ToolResult(status == "complete", text, {"status": status, **copy.deepcopy(state)})

    @staticmethod
    def _human_bytes(value):
        value = float(value)
        for unit in ("bytes", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{value:.1f} {unit}" if unit != "bytes" else f"{int(value)} bytes"
            value /= 1024

    def fork(self):
        """Share tool implementations, never grants or shutdown authorization."""
        registry = ToolRegistry()
        registry._tools = self._tools.copy()
        registry._last_download = self._last_download
        registry._download_tasks = self._download_tasks
        registry._last_command = self._last_command
        registry._command_tasks = self._command_tasks
        registry._control = self._control
        return registry

    def present_approval(self):
        # Background preparation may finish while the user is talking. The grant
        # becomes usable for two minutes only once its prompt is delivered.
        if self._pending:
            name, arguments, _ = self._pending
            self._pending = (name, arguments, time.monotonic() + 120)

    def clear_approval(self):
        self._pending = None

    @classmethod
    def is_shutdown_command(cls, text):
        return cls.normalize_command(text) in {
            "shut down", "shutdown", "shut down athena", "shutdown athena",
            "athena shut down", "athena shutdown", "stop athena", "exit athena",
            "please shut down", "please shut down athena", "athena please shut down",
            "shut down please", "athena shut down please", "shut yourself down",
        }

    async def _prepare_download(self, tool, arguments):
        if self._last_download.get("status") == "downloading":
            return ToolResult(False, "A download is already running. Ask for its download status first.")
        self._pending = None
        prepared = await asyncio.wait_for(tool.prepare(arguments), timeout=30)
        self._last_download.clear()
        self._last_download.update({"requested": copy.deepcopy(arguments), "success": False,
                                    "status": "prepared" if prepared.success else "preparation_failed",
                                    "message": prepared.spoken_text,
                                    **copy.deepcopy(prepared.data)})
        if not prepared.success:
            return prepared
        actual = prepared.data["arguments"]
        self._pending = ("download_file", copy.deepcopy(actual), time.monotonic() + 120)
        return ToolResult(False, prepared.spoken_text, {"approval_required": True,
            **actual, "metadata": prepared.data["metadata"], "expires_in_seconds": 120})

    async def _prepare_command(self, tool, arguments):
        if self._last_command.get("status") == "running":
            return ToolResult(False, "A command is already running. Ask for command status first.")
        prepared = tool.prepare(arguments)
        if asyncio.iscoroutine(prepared):
            prepared = await prepared
        actual = prepared.data.get("arguments", {})
        self._last_command.clear()
        self._last_command.update({"status": "prepared" if prepared.success else "preparation_failed",
                                   "success": False, "arguments": copy.deepcopy(actual),
                                   "message": prepared.spoken_text})
        if prepared.success:
            self._pending = ("run_command", copy.deepcopy(actual), time.monotonic() + 120)
            return ToolResult(False, prepared.spoken_text,
                              {"approval_required": True, **actual, "expires_in_seconds": 120})
        return prepared

    async def _prepare_armbian_imager(self):
        self._armbian_choice_pending = False
        self._pending = None
        asset = await self.execute("find_github_release_asset", {
            "repository": "armbian/imager", "filename_contains": "x64-setup.exe"})
        if not asset.success:
            self._retry_action = "armbian_imager"
            return asset
        prompt = await self.execute("download_file", {
            "url": asset.data["url"], "filename": asset.data["filename"]})
        self._retry_action = None if prompt.data.get("approval_required") else "armbian_imager"
        return prompt

    async def handle_user_command(self, text: str) -> ToolResult | None:
        """Called ONLY on a fresh user transcript, never on model or website text."""
        command = self.normalize_command(text)
        music = self._tools.get("netease_music")
        if music is not None:
            if (re.search(r"\b(?:pause|resume|continue|stop|skip|next)\b", command)
                    and (re.search(r"\b(?:music|song|track|netease|playing)\b", command)
                         or command in {"pause", "resume", "continue", "stop", "skip", "next"})):
                action = "pause" if "pause" in command else "resume" if re.search(r"\b(?:resume|continue)\b", command) else "stop" if "stop" in command else "next"
                return await self.execute("netease_music", {"action": action})
            play_match = re.search(r"\b(?:play|put on|listen to|switch to|change to)\s+(?:some\s+)?(.+)$", command)
            # Playing audio is the default meaning of these phrases. Route it
            # locally so stale model memory can never claim the provider is
            # unreachable without actually calling it. Explicit video/movie
            # requests remain available to future video tools.
            if play_match and not re.search(r"\b(?:video|movie|film)\b", command):
                query = play_match.group(1).strip()
                query = re.sub(r"\s+on\s+(?:netease|music)\s*$", "", query).strip()
                return await self.execute("netease_music", {"action": "play", "query": query})
        self._shutdown_authorized = self.is_shutdown_command(text)
        if self._shutdown_authorized:
            self._pending = None
            return await self.execute("shutdown_athena", {})
        if (re.search(r"\bwhat\s+(?:tools|can you do)\b", command)
                or re.search(r"\b(?:which|what)\s+tools?\s+(?:do you have|are available)\b", command)):
            capabilities = {
                "get_weather": "weather", "get_local_time": "time",
                "search_web": "web search", "read_webpage": "webpage reading",
                "browse_webpage": "Chromium browsing", "coding_workspace": "isolated coding and tests",
                "run_command": "approved computer commands", "download_file": "approved downloads",
                "manage_settings": "settings", "shutdown_athena": "assistant shutdown",
            }
            available = [label for name, label in capabilities.items() if name in self._tools]
            return ToolResult(True, "I can use " + ", ".join(available) + ".")
        asks_armbian = "armbian" in command and re.search(r"\b(download|imager|image)\b", command)
        chooses_imager = self._armbian_choice_pending and command in {
            "windows", "windows one", "windows version", "the windows one",
            "imager", "the imager", "windows imager", "the windows imager", "imager for windows"}
        chooses_os_image = self._armbian_choice_pending and command in {
            "image", "os image", "operating system image", "armbian image", "the os image"}
        if command in {"try again", "retry", "retry it", "try that again"} and self._retry_action:
            if self._retry_action == "armbian_imager":
                return await self._prepare_armbian_imager()
        if (self._last_download.get("status") == "redirected"
                and not self.is_download_status_query(text)
                and re.search(r"\b(?:continue|retry|resume|download)\b", command)):
            redirected = copy.deepcopy(self._last_download.get("redirect_request", {}))
            tool = self._tools.get("download_file")
            if tool is None or not redirected:
                return ToolResult(False, "I no longer have the redirected download details. Ask for the file again.")
            return await self._prepare_download(tool, redirected)
        if chooses_os_image:
            self._armbian_choice_pending = False
            return ToolResult(False, "Which exact board model and Armbian edition do you need?")
        if asks_armbian or chooses_imager:
            if "imager" not in command and not chooses_imager:
                self._armbian_choice_pending = True
                self._pending = None
                return ToolResult(False,
                    "Do you mean the Armbian Imager for Windows, or an Armbian operating-system image?")
            return await self._prepare_armbian_imager()  # Inline approval still cannot skip inspection.
        if self.is_download_status_query(text):
            return self.download_status()
        if self.is_command_status_query(text):
            return self.command_status()
        if re.search(r"\b(test|check|diagnose|working|works|work|try|use)\b", command) and re.search(
            r"\b(internet|web|browsing|browser|search)\b", command
        ):
            self._pending = None
            if "search" in command:
                result = await self.execute("search_web", {
                    "query": "Python official documentation", "limit": 3})
                count = len(result.data.get("results", []))
                return ToolResult(result.success,
                    f"Web search test {'passed' if result.success else 'failed'}; it returned {count} usable result{'s' if count != 1 else ''}.",
                    {"diagnostic": "search_web", "result": copy.deepcopy(result.data)})
            result = await self.execute("browse_webpage", {
                "url": "https://example.com/", "max_chars": 2000})
            has_marker = "Example Domain" in result.data.get("text", "")
            passed = result.success and has_marker
            return ToolResult(passed,
                "Internet browser test passed; Chromium loaded and read the expected page."
                if passed else "Internet browser test failed; Chromium did not return the expected page.",
                {"diagnostic": "browse_webpage", "tool_success": result.success,
                 "expected_content": has_marker, "result": copy.deepcopy(result.data)})
        if command in self.APPROVE:
            pending, self._pending = self._pending, None  # One use, even on failure.
            if pending is None:
                return ToolResult(False, "There is no real action waiting for approval.")
            name, arguments, expires = pending
            if time.monotonic() >= expires:
                return ToolResult(False, "That approval expired. Ask for the action again.")
            started = time.monotonic()
            if name == "run_command":
                self._last_command.update({"status": "running", "success": False,
                    "arguments": copy.deepcopy(arguments), "started_at": datetime.now(timezone.utc).isoformat(),
                    "message": "The command is running."})
                task = asyncio.create_task(self._finish_command(name, arguments))
                self._command_tasks.add(task)
                task.add_done_callback(self._command_tasks.discard)
                return ToolResult(True, "Command started in the background. You can keep chatting and ask for command status.",
                                  {"command_started": True})
            metadata = self._last_download.get("metadata", {})
            self._last_download.update({"status": "downloading", "success": False,
                                        "arguments": copy.deepcopy(arguments),
                                        "bytes_downloaded": 0,
                                        "bytes_total": metadata.get("bytes"),
                                        "bytes_per_second": 0,
                                        "started_at": datetime.now(timezone.utc).isoformat(),
                                        "message": f"Downloading {arguments.get('filename', 'the file')}."})
            def progress(received, total):
                elapsed = max(0.001, time.monotonic() - started)
                self._last_download.update(bytes_downloaded=received,
                    bytes_total=total or self._last_download.get("bytes_total"),
                    bytes_per_second=received / elapsed)
            task = asyncio.create_task(self._finish_download(name, arguments, progress))
            self._download_tasks.add(task)
            task.add_done_callback(self._download_tasks.discard)
            return ToolResult(True,
                f"Download started for {arguments.get('filename', 'the file')}. You can keep chatting and ask for download progress.",
                {"download_started": True, "filename": arguments.get("filename")})
        if command in self.DENY and self._pending:
            name = self._pending[0]
            self._pending = None
            if name == "run_command":
                self._last_command["status"] = "cancelled"
                return ToolResult(True, "Command cancelled.")
            self._last_download["status"] = "cancelled"
            return ToolResult(True, "Download cancelled.")
        if self._last_download and re.search(r"\b(url|link)\b", command) and re.search(r"\b(show|give|what|which)\b", command):
            url = self._last_download.get("metadata", {}).get("url")
            if url:
                return ToolResult(True, "I've printed the inspected URL in the terminal.", {"display_url": url})
        if self._pending and command in {"okay", "ok"}:
            action = "command" if self._pending[0] == "run_command" else "download"
            return ToolResult(False, f"Say yes to approve this {action}, or no to cancel.")
        # A different request invalidates old permission; it cannot authorize a later job.
        self._pending = None
        return None

    async def _finish_download(self, name, arguments, progress):
        try:
            tool = self._tools[name]
            execute = getattr(tool, "execute_with_progress", None)
            if execute is not None:
                result = await asyncio.wait_for(execute(arguments, progress),
                                                timeout=tool.definition.timeout_seconds)
            else:
                result = await self.execute(name, arguments, confirmed=True)
            self._last_download.update({"status": "complete" if result.success else "failed",
                "success": result.success, "result": copy.deepcopy(result.data),
                "message": result.spoken_text,
                "finished_at": datetime.now(timezone.utc).isoformat()})
            if result.data.get("redirect_url"):
                updated = {**arguments, "url": result.data["redirect_url"]}
                self._last_download.update({
                    "status": "redirected",
                    "success": False,
                    "redirect_request": copy.deepcopy(updated),
                    "message": (
                        f"The download of {arguments.get('filename', 'the file')} paused because "
                        "the destination changed to another server. Ask me to continue the download; "
                        "I will inspect the new destination and request fresh approval."
                    ),
                })
        except asyncio.CancelledError:
            self._last_download.update({"status": "cancelled", "success": False,
                "message": "The download was interrupted and its partial file was removed."})
            raise
        except Exception:
            self._last_download.update({"status": "failed", "success": False,
                "message": "The background download failed."})

    async def _finish_command(self, name, arguments):
        try:
            result = await self.execute(name, arguments, confirmed=True)
            status = result.data.get("status", "complete" if result.success else "failed")
            self._last_command.update({"status": status, "success": result.success,
                "result": copy.deepcopy(result.data), "message": result.spoken_text,
                "finished_at": datetime.now(timezone.utc).isoformat()})
        except asyncio.CancelledError:
            self._last_command.update({"status": "cancelled", "success": False,
                                       "message": "The command was interrupted."})
            raise
        except Exception:
            self._last_command.update({"status": "failed", "success": False,
                                       "message": "The background command failed."})

    async def close(self):
        tasks = [*self._download_tasks, *self._command_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_for_downloads(self):
        await asyncio.gather(*list(self._download_tasks), return_exceptions=True)

    async def wait_for_commands(self):
        await asyncio.gather(*list(self._command_tasks), return_exceptions=True)

    @classmethod
    def discover(
        cls,
        package_name: str = "athena.tools",
        services: dict[str, Any] | None = None,
    ) -> "ToolRegistry":
        registry = cls()
        package = importlib.import_module(package_name)
        for module_info in pkgutil.iter_modules(package.__path__):
            if module_info.name.startswith("_") or module_info.name in {
                "models",
                "registry",
            }:
                continue
            module = importlib.import_module(f"{package_name}.{module_info.name}")
            factory = getattr(module, "create_tools", None)
            candidates = factory() if factory else getattr(module, "TOOLS", [])
            if not candidates:
                tool = getattr(module, "TOOL", None)
                candidates = [tool] if tool is not None else []
            for tool in candidates:
                bind = getattr(tool, "bind", None)
                if bind is not None:
                    bind(services or {})
                registry.register(tool)
        return registry

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if not name.isidentifier():
            raise ValueError(f"Invalid tool name: {name}")
        if name in self._tools:
            raise ValueError(f"Duplicate tool name: {name}")
        Draft202012Validator.check_schema(tool.definition.parameters)
        self._tools[name] = tool

    def definitions(self, names=None) -> list[dict[str, Any]]:
        allowed = set(names) if names is not None else None
        return [tool.definition.for_model() for name, tool in self._tools.items()
                if allowed is None or name in allowed]

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        confirmed: bool = False,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, f"The tool {name} is not available.")
        definition = tool.definition
        self._validate_arguments(definition.parameters, arguments)
        if name == "shutdown_athena" and not self._shutdown_authorized:
            return ToolResult(False, "Say shut down ATHENA to stop the assistant.")
        if definition.permission is not PermissionLevel.SAFE and not confirmed:
            if name == "download_file":
                try:
                    return await self._prepare_download(tool, arguments)
                except TimeoutError:
                    return ToolResult(False, "Download inspection timed out. No file was downloaded.")
            if name == "run_command":
                try:
                    return await self._prepare_command(tool, arguments)
                except ValueError as error:
                    return ToolResult(False, f"I won't run that command. {error}")
            return ToolResult(False, "This action requires confirmation.")
        try:
            result = await asyncio.wait_for(
                tool.execute(arguments), timeout=definition.timeout_seconds
            )
            if name == "shutdown_athena" and result.success:
                self.shutdown_requested = True
            return result
        except TimeoutError:
            return ToolResult(False, f"The {name} tool timed out.")

    @staticmethod
    def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        errors = list(Draft202012Validator(schema).iter_errors(arguments))
        if errors:
            error = errors[0]
            field = ".".join(str(part) for part in error.absolute_path) or "arguments"
            raise ValueError(f"Invalid tool {field}: violates {error.validator} constraint")
