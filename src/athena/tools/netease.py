"""NetEase Cloud Music playback for mainland-China networks."""
from __future__ import annotations

import asyncio
from array import array
import json
import os
import signal
import sys
from typing import Any

import httpx

from athena.tools.models import ToolDefinition, ToolResult


class NetEasePlayer:
    def __init__(self, speaker) -> None:
        self.speaker = speaker
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.queue: list[tuple[str, str]] = []
        self.current_title = ""
        self.paused = False
        self._lock = asyncio.Lock()
        self._voice_clear = asyncio.Event()
        self._voice_clear.set()
        self._started = asyncio.Event()
        self.last_error = ""
        self.volume = max(0.0, min(1.0, float(os.environ.get("ATHENA_MUSIC_VOLUME", "0.35"))))

    def suspend_for_voice(self) -> None:
        self._voice_clear.clear()

    def resume_after_voice(self) -> None:
        self._voice_clear.set()

    def set_volume(self, percent: int) -> None:
        self.volume = max(0.0, min(1.0, int(percent) / 100.0))

    def status(self) -> dict[str, Any]:
        playing = self.task is not None and not self.task.done()
        return {
            "playing": playing,
            "paused": playing and self.paused,
            "title": self.current_title,
            "volume": round(self.volume * 100),
            "error": self.last_error,
        }

    def _apply_volume(self, pcm: bytes) -> bytes:
        if self.volume >= 0.999:
            return pcm
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        for index, sample in enumerate(samples):
            samples[index] = int(sample * self.volume)
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes()

    async def _search(self, query: str) -> list[tuple[str, str]]:
        url = "https://music.163.com/api/search/get/web"
        try:
            async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as client:
                response = await client.get(url, params={"s": query, "type": 1, "limit": 5, "offset": 0})
                response.raise_for_status()
                songs = response.json().get("result", {}).get("songs", [])
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError(f"NetEase Music search failed: {error}") from None
        results = []
        for song in songs:
            song_id = song.get("id")
            name = song.get("name") or "NetEase track"
            artists = ", ".join(a.get("name", "") for a in song.get("artists", []))
            if song_id:
                results.append((str(song_id), f"{name} - {artists}".strip(" -")))
        if not results:
            raise RuntimeError("NetEase Music returned no matching tracks.")
        return results

    async def _stream_url(self, song_id: str) -> str:
        endpoint = "https://music.163.com/api/song/enhance/player/url/v1"
        try:
            async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as client:
                response = await client.get(endpoint, params={
                    "ids": json.dumps([int(song_id)]), "level": "standard", "encodeType": "mp3"
                })
                response.raise_for_status()
                data = response.json().get("data", [])
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError(f"NetEase Music could not get a stream: {error}") from None
        stream = data[0].get("url") if data else None
        # The public outer URL is a useful anonymous fallback for tracks whose
        # player endpoint omits a URL.  ffmpeg follows its redirect.
        return stream or f"https://music.163.com/song/media/outer/url?id={int(song_id)}.mp3"

    async def play(self, query: str) -> None:
        async with self._lock:
            await self.stop()
            self.queue = await self._search(query)
            self.last_error = ""
            self._started.clear()
            self.task = asyncio.create_task(self._run_queue())
            try:
                await asyncio.wait_for(self._started.wait(), 15)
            except asyncio.TimeoutError:
                await self.stop()
                raise RuntimeError("NetEase took too long to start playback.") from None
            if self.last_error:
                raise RuntimeError(self.last_error)

    async def _run_queue(self) -> None:
        try:
            while self.queue:
                song_id, title = self.queue.pop(0)
                stream_url = await self._stream_url(song_id)
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
                    self._started.set()
                    await self._voice_clear.wait()
                    await self.speaker.play(self._apply_volume(chunk))
                await self.process.wait()
                self.process = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = f"NetEase playback failed: {error}"
            self._started.set()
            print(f"NetEase playback failed: {error}", flush=True)
        finally:
            process, self.process = self.process, None
            if process is not None and process.returncode is None:
                process.kill(); await process.wait()
            self.paused = False
            if not self._started.is_set() and not self.last_error:
                self.last_error = "NetEase returned no playable audio for those results."
            self._started.set()

    async def pause(self) -> None:
        if self.process is None:
            raise RuntimeError("No NetEase track is playing.")
        if not self.paused:
            self.process.send_signal(signal.SIGSTOP); self.paused = True

    async def resume(self) -> None:
        if self.process is None:
            raise RuntimeError("No NetEase track is playing.")
        if self.paused:
            self.process.send_signal(signal.SIGCONT); self.paused = False

    async def next(self) -> None:
        if self.process is None and not self.queue:
            raise RuntimeError("No NetEase track is playing.")
        if self.process is not None:
            self.process.terminate()
        self.paused = False

    async def stop(self) -> None:
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        if self.process is not None and self.process.returncode is None:
            self.process.kill(); await self.process.wait()
        self.process = None; self.queue.clear(); self.current_title = ""; self.paused = False


class NetEaseMusicTool:
    definition = ToolDefinition(
        name="netease_music",
        description=("Play music from NetEase Cloud Music on the ATHENA speaker. "
                     "Actions are play, pause, resume, next, or stop. Playback runs in the background."),
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["play", "pause", "resume", "next", "stop"]},
            "query": {"type": "string"}}, "required": ["action"], "additionalProperties": False},
    )

    def __init__(self) -> None:
        self.player: NetEasePlayer | None = None

    def bind(self, services: dict[str, Any]) -> None:
        player = services.get("netease_player")
        if isinstance(player, NetEasePlayer):
            self.player = player

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if self.player is None:
            return ToolResult(False, "NetEase Music is unavailable in this interface.")
        try:
            action = arguments["action"]
            if action == "play":
                query = str(arguments.get("query", "")).strip()
                if not query or len(query) > 300:
                    return ToolResult(False, "Tell me which song or artist to play.")
                await self.player.play(query)
                title = self.player.current_title or (self.player.queue[0][1] if self.player.queue else query)
                return ToolResult(True, f"Playing {title} on the ATHENA speaker.")
            if action == "pause":
                await self.player.pause(); return ToolResult(True, "Music paused.")
            if action == "resume":
                await self.player.resume(); return ToolResult(True, "Music resumed.")
            if action == "next":
                await self.player.next(); return ToolResult(True, "Skipping to the next track.")
            await self.player.stop(); return ToolResult(True, "Music stopped.")
        except (RuntimeError, OSError) as error:
            return ToolResult(False, str(error))


TOOL = NetEaseMusicTool()
