"""Background YouTube music playback through ATHENA's configured speaker."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import shutil
from typing import Any

from athena.tools.models import ToolDefinition, ToolResult


class YouTubePlayer:
    def __init__(self, speaker) -> None:
        self.speaker = speaker
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.queue: list[tuple[str, str]] = []
        self.current_title = ""
        self.last_query = ""
        self.paused = False
        self._lock = asyncio.Lock()

    @staticmethod
    def _yt() -> list[str]:
        executable = shutil.which("yt-dlp")
        return [executable] if executable else [os.environ.get("PYTHON", "python3"), "-m", "yt_dlp"]

    async def _search(self, query: str) -> list[tuple[str, str]]:
        command = self._yt() + ["--flat-playlist", "--playlist-end", "5",
                                "--print", "%(webpage_url)s\t%(title)s",
                                f"ytsearch5:{query}"]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(), 20)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("YouTube search timed out.") from None
        if process.returncode:
            raise RuntimeError("YouTube search failed. Check that yt-dlp is installed and the Pi can reach YouTube.")
        results = []
        for line in output.decode(errors="replace").splitlines():
            if "\t" in line:
                url, title = line.split("\t", 1)
                if url.startswith("http"):
                    results.append((url.strip(), title.strip() or "YouTube track"))
        if not results:
            raise RuntimeError("YouTube returned no playable tracks.")
        return results

    async def play(self, query: str) -> None:
        async with self._lock:
            await self.stop()
            self.last_query = query
            self.queue = await self._search(query)
            self.task = asyncio.create_task(self._run_queue())

    async def _run_queue(self) -> None:
        try:
            while self.queue:
                url, title = self.queue.pop(0)
                command = self._yt() + ["-f", "bestaudio/best", "--get-url", "--no-playlist", url]
                resolver = await asyncio.create_subprocess_exec(
                    *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                output, _ = await asyncio.wait_for(resolver.communicate(), 20)
                stream_url = output.decode(errors="replace").strip().splitlines()[-1]
                self.current_title = title
                self.process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-loglevel", "error", "-i", stream_url,
                    "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "24000", "pipe:1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                self.paused = False
                while self.process.stdout is not None:
                    chunk = await self.process.stdout.read(4800)
                    if not chunk:
                        break
                    await self.speaker.play(chunk)
                await self.process.wait()
                self.process = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"YouTube playback failed: {error}", flush=True)
        finally:
            process, self.process = self.process, None
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self.paused = False

    async def pause(self) -> None:
        if self.process is None:
            raise RuntimeError("No YouTube track is playing.")
        if not self.paused:
            self.process.send_signal(signal.SIGSTOP)
            self.paused = True

    async def resume(self) -> None:
        if self.process is None:
            raise RuntimeError("No YouTube track is playing.")
        if self.paused:
            self.process.send_signal(signal.SIGCONT)
            self.paused = False

    async def next(self) -> None:
        if self.process is None and not self.queue:
            raise RuntimeError("No YouTube track is playing.")
        if self.process is not None:
            self.process.terminate()
        self.paused = False

    async def stop(self) -> None:
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        self.process = None
        self.queue.clear()
        self.current_title = ""
        self.paused = False


class YouTubeMusicTool:
    definition = ToolDefinition(
        name="youtube_music",
        description=("Play music or a song from YouTube on the ATHENA speaker. "
                     "Actions are play, pause, resume, next, or stop. Playback runs in the background."),
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["play", "pause", "resume", "next", "stop"]},
            "query": {"type": "string"}}, "required": ["action"], "additionalProperties": False},
    )

    def __init__(self) -> None:
        self.player: YouTubePlayer | None = None

    def bind(self, services: dict[str, Any]) -> None:
        player = services.get("youtube_player")
        if isinstance(player, YouTubePlayer):
            self.player = player

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if self.player is None:
            return ToolResult(False, "YouTube music is unavailable in this interface.")
        action = arguments["action"]
        try:
            if action == "play":
                query = str(arguments.get("query", "")).strip()
                if not query or len(query) > 300:
                    return ToolResult(False, "Tell me which song or artist to play.")
                await self.player.play(query)
                return ToolResult(True, f"Playing {self.player.queue[0][1] if self.player.queue else query} on the ATHENA speaker.")
            if action == "pause":
                await self.player.pause(); return ToolResult(True, "YouTube playback paused.")
            if action == "resume":
                await self.player.resume(); return ToolResult(True, "YouTube playback resumed.")
            if action == "next":
                await self.player.next(); return ToolResult(True, "Skipping to the next track.")
            await self.player.stop(); return ToolResult(True, "YouTube playback stopped.")
        except (RuntimeError, OSError) as error:
            return ToolResult(False, str(error))


# Kept only as a compatibility module for older local tests/configurations.
# It is deliberately not registered; ATHENA uses NetEase Cloud Music instead.
TOOL = None
