"""Background YouTube playback through ATHENA's configured speaker, audio only.

Audio is decoded from the best available stream and piped straight to the
speaker, so a video plays as its soundtrack without ever downloading or
showing picture. Playback runs in the background and ducks under Athena's
voice using the same contract NetEase does, because both share one speaker.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from typing import Any

from athena.tools.models import ToolDefinition, ToolResult


def _looks_like_url(query: str) -> bool:
    return query.startswith(("http://", "https://", "www.", "youtu.be", "youtube.com"))


class YouTubePlayer:
    def __init__(self, speaker) -> None:
        self.speaker = speaker
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.queue: list[tuple[str, str]] = []
        self.current_title = ""
        self.last_query = ""
        self.paused = False
        self.last_error = ""
        self._lock = asyncio.Lock()
        self._output_lock = asyncio.Lock()
        # Voice ducking, mirroring NetEase: writes wait on these instead of
        # racing Athena's speech for the same audio stream.
        self._voice_depth = 0
        self._voice_clear = asyncio.Event()
        self._voice_clear.set()
        self._playback_clear = asyncio.Event()
        self._playback_clear.set()
        # Set once audio has actually reached the speaker, so `play` can wait
        # for proof rather than returning the instant a background task exists.
        self._started = asyncio.Event()
        self._generation = 0

    @staticmethod
    def _yt() -> list[str]:
        """The command that runs yt-dlp.

        The release installs yt-dlp into ATHENA's own virtualenv, which is not on
        the service's PATH, so a bare `python3 -m yt_dlp` would reach the system
        interpreter and fail. The interpreter running this process is the one that
        definitely has the module, so it is preferred over a PATH lookup.
        """
        executable = shutil.which("yt-dlp")
        if executable:
            return [executable]
        if getattr(sys,'frozen',False):
            return [sys.executable,'--worker','yt-dlp']
        return [sys.executable or "python3", "-m", "yt_dlp"]

    def _music_format(self) -> tuple[int, int]:
        """The rate and channel count playback should be decoded to.

        Whatever is carrying the sound decides, so the decoder and the output
        cannot disagree — that shows up as the wrong pitch or half a track.
        """
        format_of = getattr(self.speaker, "music_format", None)
        if format_of is None:
            return 24_000, 1
        rate, channels = format_of
        return int(rate), int(channels)

    def _describe(self, url: str) -> str:
        command = self._yt() + ["--skip-download", "--no-playlist",
                                "--print", "%(title)s", url]
        import subprocess
        try:
            process = subprocess.run(command, capture_output=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError):
            return "that video"
        title = process.stdout.decode(errors="replace").strip().splitlines()
        return title[-1] if title and title[-1] else "that video"

    async def _search(self, query: str) -> list[tuple[str, str]]:
        command = self._yt() + ["--flat-playlist", "--playlist-end", "5",
                                "--print", "%(webpage_url)s\t%(title)s",
                                f"ytsearch5:{query}"]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 25)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("YouTube search timed out.") from None
        if process.returncode:
            raise RuntimeError(
                "YouTube search failed. Check that yt-dlp is installed and the "
                "board can reach YouTube.")
        results = []
        for line in output.decode(errors="replace").splitlines():
            if "\t" in line:
                url, title = line.split("\t", 1)
                if url.startswith("http"):
                    results.append((url.strip(), title.strip() or "YouTube video"))
        if not results:
            raise RuntimeError("YouTube returned no playable videos.")
        return results

    async def play(self, query: str) -> None:
        """Start playback for a search phrase or a direct YouTube link.

        Returns only once audio is genuinely reaching the speaker. Starting a
        background task proves nothing: `yt-dlp` can resolve the video and
        `ffmpeg` can still fail on the stream, and every one of those failures
        happens after this call would otherwise have returned. Reporting
        success there is what made this look like it was playing while the room
        stayed silent.
        """
        query = query.strip()
        if not query:
            raise RuntimeError("Tell me which video to play.")
        if getattr(self.speaker, "available", True) is False:
            # Without this the search and the download both succeed, the tool
            # reports success, and the track is then handed to an output that
            # cannot play it — which is exactly the "it said it was playing and
            # nothing came out" case.
            raise RuntimeError("No audio output is available for YouTube right now.")
        async with self._lock:
            await self.stop()
            self.last_query = query
            self.last_error = ""
            self._started.clear()
            # A pasted link is played as-is. Searching for a URL finds nothing,
            # because YouTube does not index links as titles.
            self.queue = ([(query, await self._title_for(query))]
                          if _looks_like_url(query) else await self._search(query))
            self._generation += 1
            self.task = asyncio.create_task(self._run_queue(self._generation))
            try:
                await asyncio.wait_for(self._started.wait(), 30)
            except asyncio.TimeoutError:
                await self.stop()
                raise RuntimeError(
                    "YouTube took too long to start. Check that yt-dlp and ffmpeg "
                    "are installed and that the board can reach YouTube.") from None
            if self.last_error:
                error, self.last_error = self.last_error, ""
                await self.stop()
                raise RuntimeError(error)

    async def _title_for(self, url: str) -> str:
        return await asyncio.to_thread(self._describe, url)

    async def _resolve(self, url: str) -> str:
        command = self._yt() + ["-f", "bestaudio/best", "--get-url", "--no-playlist", url]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, _ = await asyncio.wait_for(process.communicate(), 25)
        lines = [line.strip() for line in output.decode(errors="replace").splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("YouTube returned no audio stream for that video.")
        return lines[-1]

    async def _write(self, pcm: bytes, rate: int, channels: int,
                     generation: int, describes_itself: bool) -> bool:
        """Hand one chunk to the speaker, waiting for the voice to finish first."""
        while True:
            await self._voice_clear.wait()
            await self._playback_clear.wait()
            async with self._output_lock:
                if generation != self._generation:
                    return False
                # Re-checked under the lock: Athena may have started speaking
                # while this chunk waited for the stream.
                if not self._voice_clear.is_set() or not self._playback_clear.is_set():
                    continue
                if describes_itself:
                    await self.speaker.play(pcm, rate, channels)
                else:
                    await self.speaker.play(pcm)
                return True

    async def _run_queue(self, generation: int) -> None:
        try:
            rate, channels = self._music_format()
            # Only a speaker that advertises a music format can be told one.
            describes_itself = getattr(self.speaker, "music_format", None) is not None
            chunk_bytes = max(2, rate * channels * 2 // 10)
            while self.queue and generation == self._generation:
                url, title = self.queue.pop(0)
                stream_url = await self._resolve(url)
                self.current_title = title
                self.process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-loglevel", "error", "-i", stream_url,
                    "-vn", "-f", "s16le", "-acodec", "pcm_s16le",
                    "-ac", str(channels), "-ar", str(rate), "pipe:1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                self.paused = False
                self._playback_clear.set()
                while self.process.stdout is not None:
                    chunk = await self.process.stdout.read(chunk_bytes)
                    if not chunk:
                        break
                    if not await self._write(chunk, rate, channels,
                                             generation, describes_itself):
                        return
                    # First audio has been handed to the speaker, so `play` can
                    # report success honestly rather than on task creation.
                    self._started.set()
                await self.process.wait()
                self.process = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = str(error)
            print(f"YouTube playback failed: {error}", flush=True)
        finally:
            process, self.process = self.process, None
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self.paused = False
            # A queue that ran dry without ever producing audio is a failure,
            # not a success that happens to have finished quickly.
            if not self._started.is_set() and not self.last_error:
                self.last_error = (
                    "YouTube returned no playable audio for that video. The board may "
                    "need ffmpeg, or YouTube refused the stream.")
            self._started.set()

    async def pause(self) -> None:
        if not self.playing:
            raise RuntimeError("No YouTube video is playing.")
        if not self.paused:
            self._playback_clear.clear()
            self.paused = True

    async def resume(self) -> None:
        if not self.playing:
            raise RuntimeError("No YouTube video is playing.")
        if self.paused:
            self._playback_clear.set()
            self.paused = False

    async def next(self) -> None:
        if not self.playing and not self.queue:
            raise RuntimeError("No YouTube video is playing.")
        self.paused = False
        self._playback_clear.set()
        if self.process is not None:
            self.process.terminate()

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
        self._generation += 1
        self._playback_clear.set()
        # Cleared last: a stopped player must not look like one that started,
        # or the next `play` returns immediately on a stale event.
        self._started.clear()

    @property
    def playing(self) -> bool:
        return self.task is not None and not self.task.done()

    def status(self) -> dict[str, Any]:
        playing = self.playing
        return {
            "playing": playing,
            "paused": playing and self.paused,
            "title": self.current_title,
            "queued": len(self.queue),
            "error": self.last_error,
        }

    def describe(self) -> str:
        state = self.status()
        if not state["playing"]:
            return "Nothing is playing from YouTube."
        if state["paused"]:
            return f"YouTube is paused on {state['title']}."
        return f"YouTube is playing {state['title']}."

    # -- voice ducking, the contract the coordinator's audio focus uses --

    def suspend_for_voice(self) -> None:
        self._voice_depth += 1
        self._voice_clear.clear()

    def resume_after_voice(self) -> None:
        self._voice_depth = max(0, self._voice_depth - 1)
        if self._voice_depth == 0:
            self._voice_clear.set()

    async def wait_for_voice(self) -> None:
        """Finish at most one queued write, then drop what is still buffered."""
        async with self._output_lock:
            if self._voice_depth == 1 and self.task is not None and not self.task.done():
                await self.speaker.stop()


class YouTubeMusicTool:
    definition = ToolDefinition(
        name="youtube_audio",
        description=(
            "Play a YouTube video as audio only on the ATHENA speaker — no picture "
            "is downloaded or shown. Accepts a search phrase, a video title, or a "
            "pasted YouTube link. Actions are play, pause, resume, next, status, or "
            "stop. Playback runs in the background and keeps playing while Athena "
            "talks."
        ),
        parameters={"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["play", "pause", "resume", "next", "status", "stop"]},
            "query": {"type": "string", "maxLength": 500}},
         "required": ["action"], "additionalProperties": False},
        timeout_seconds=45,
    )

    def __init__(self) -> None:
        self.player: YouTubePlayer | None = None

    def bind(self, services: dict[str, Any]) -> None:
        player = services.get("youtube_player")
        if isinstance(player, YouTubePlayer):
            self.player = player

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if self.player is None:
            return ToolResult(False, "YouTube playback is unavailable in this interface.")
        action = arguments["action"]
        try:
            if action == "play":
                query = str(arguments.get("query", "")).strip()
                if not query or len(query) > 500:
                    return ToolResult(False, "Tell me which video to play, or paste the link.")
                await self.player.play(query)
                # The queue is drained by the background task, so the title is
                # whatever the player has resolved so far, else the query.
                title = (self.player.current_title
                         or (self.player.queue[0][1] if self.player.queue else "")
                         or query)
                return ToolResult(True, f"Playing {title} from YouTube, audio only.",
                                  {"status": self.player.status()})
            if action == "status":
                return ToolResult(True, self.player.describe(), {"status": self.player.status()})
            if action == "pause":
                await self.player.pause()
                return ToolResult(True, "YouTube paused.", {"status": self.player.status()})
            if action == "resume":
                await self.player.resume()
                return ToolResult(True, "YouTube resumed.", {"status": self.player.status()})
            if action == "next":
                await self.player.next()
                return ToolResult(True, "Skipping to the next video.", {"status": self.player.status()})
            await self.player.stop()
            return ToolResult(True, "YouTube stopped.", {"status": self.player.status()})
        except (RuntimeError, OSError) as error:
            return ToolResult(False, str(error))


def create_tools():
    return [YouTubeMusicTool()]
