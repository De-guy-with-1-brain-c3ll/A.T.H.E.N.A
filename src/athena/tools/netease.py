"""NetEase Cloud Music playback for mainland-China networks.

Saved playlists are name + search query + optional mood tags. `choose_playlist`
picks one from a request, and the clock only ever *biases* tags the user wrote
themselves — asking for "a playlist" at midnight and at noon should not land on
the same thing, but an explicit mood in the request always outranks the hour.
"""
from __future__ import annotations

import asyncio
from array import array
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import httpx

from athena.paths import data_directory
from athena.tools.models import ToolDefinition, ToolResult


# Mood tags that suit each part of the day, as a small, explainable table rather
# than a model: it has to be predictable enough that the choice can be reported
# back in a sentence, and cheap enough to run on the Pi at every request. The two
# night windows hold the same tags, so 23:00 and 02:00 cannot disagree.
_NIGHT_MOODS = ("night", "late", "calm", "quiet", "sleep", "ambient", "lofi")

TIME_MOODS: tuple[tuple[tuple[int, int], tuple[str, ...]], ...] = (
    ((5, 11), ("morning", "sunrise", "upbeat", "energy", "energetic", "wake",
               "happy", "fresh", "bright")),
    ((11, 17), ("focus", "work", "study", "concentrate", "deep", "instrumental",
                "productive")),
    ((17, 22), ("evening", "chill", "relax", "unwind", "dinner", "jazz",
                "mellow", "smooth")),
    ((22, 24), _NIGHT_MOODS),
    ((0, 5), _NIGHT_MOODS),
)


def preferred_moods(hour: int) -> tuple[str, ...]:
    """The mood tags worth favouring at this hour."""
    for (start, end), moods in TIME_MOODS:
        if start <= hour < end:
            return moods
    return ()


def hour_label(hour: int) -> str:
    for (start, end), label in (
        ((5, 11), "morning"),
        ((11, 17), "afternoon"),
        ((17, 22), "evening"),
        ((22, 24), "late night"),
        ((0, 5), "late night"),
    ):
        if start <= hour < end:
            return label
    return "day"


def same_word(left: str, right: str) -> bool:
    """Whether two words mean the same thing for playlist matching.

    A plain set intersection misses pairs a person would consider obvious —
    "energetic" against a tag of "energy", "studying" against "study" — so a
    shared opening counts, provided it is both long enough and a solid majority
    of the shorter word. That is tight enough not to pair unrelated words by
    accident, and needs no stemming library on the board.
    """
    if left == right:
        return True
    common = 0
    for left_letter, right_letter in zip(left, right):
        if left_letter != right_letter:
            break
        common += 1
    return common >= 4 and common >= 0.6 * min(len(left), len(right))


def words_matching(words: set[str], vocabulary: set[str]) -> set[str]:
    """The request words that mean something in `vocabulary`."""
    return {word for word in words
            if any(same_word(word, other) for other in vocabulary)}


class NetEasePlayer:
    def __init__(self, speaker, playlist_path: Path | None = None) -> None:
        self.speaker = speaker
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.queue: list[tuple[str, str]] = []
        self.current_title = ""
        self.paused = False
        self._lock = asyncio.Lock()
        self._voice_clear = asyncio.Event()
        self._voice_clear.set()
        self._voice_depth = 0
        self._output_lock = asyncio.Lock()
        self._playback_clear = asyncio.Event()
        self._playback_clear.set()
        self._track_generation = 0
        self._started = asyncio.Event()
        self.last_error = ""
        self.volume = max(0.0, min(1.0, float(os.environ.get("ATHENA_MUSIC_VOLUME", "0.35"))))
        # Scaling table for the current level, built on first use and dropped
        # whenever the level changes. Music is scaled ~10 chunks a second, so
        # the per-sample Python multiply was real work on the Pi.
        self._volume_table: list[int] | None = None
        self._playlist_path = playlist_path or data_directory() / "music_playlists.json"
        self._playlists: dict[str, dict[str, Any]] = {}
        self._playlist_cursor = 0
        # Why the last automatic choice was made, so it can be said out loud
        # instead of leaving the user wondering where the music came from.
        self.last_choice_reason = ""
        # Higher than the hardcoded "standard" (about 128 kbps) this used to
        # ask for. The board no longer has to be the weak link too: the stream
        # goes to a browser that decodes it natively, so asking for a good
        # source actually buys something.
        self.level = os.environ.get("ATHENA_MUSIC_LEVEL", "exhigh").strip() or "exhigh"
        self._load_playlists()

    @property
    def output_label(self) -> str:
        return ("the connected computer's default audio device"
                if self.speaker.__class__.__name__ == "BrowserSpeaker"
                else "the ATHENA speaker")

    def _load_playlists(self) -> None:
        try:
            raw = json.loads(self._playlist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        for name, entry in raw.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            query = entry.get("query")
            moods = entry.get("moods", [])
            if isinstance(query, str) and query.strip() and isinstance(moods, list):
                self._playlists[name] = {
                    "query": query.strip(),
                    "moods": [str(mood).strip() for mood in moods if str(mood).strip()],
                }

    def _save_playlists(self) -> None:
        self._playlist_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._playlist_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._playlists, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        temporary.replace(self._playlist_path)

    @staticmethod
    def _playlist_key(name: str) -> str:
        return " ".join(name.casefold().split())

    def _named_playlist(self, name: str) -> tuple[str, dict[str, Any]] | None:
        wanted = self._playlist_key(name)
        for saved_name, entry in self._playlists.items():
            if self._playlist_key(saved_name) == wanted:
                return saved_name, entry
        return None

    def add_playlist(self, name: str, query: str, moods: list[str] | None = None) -> str:
        name, query = name.strip(), query.strip()
        if not name or len(name) > 80 or not query or len(query) > 300:
            raise RuntimeError("A playlist needs a short name and a music search query.")
        existing = self._named_playlist(name)
        saved_name = existing[0] if existing else name
        self._playlists[saved_name] = {
            "query": query,
            "moods": [str(mood).strip() for mood in (moods or []) if str(mood).strip()][:12],
        }
        self._save_playlists()
        return saved_name

    def remove_playlist(self, name: str) -> str:
        found = self._named_playlist(name)
        if found is None:
            raise RuntimeError(f"I don't have a playlist named {name!r}.")
        del self._playlists[found[0]]
        self._save_playlists()
        return found[0]

    def playlists(self) -> list[dict[str, Any]]:
        """Every saved playlist, in a shape the dashboard can render directly."""
        return [{"name": name, "query": entry["query"], "moods": list(entry["moods"])}
                for name, entry in self._playlists.items()]

    def playlist_summary(self) -> str:
        if not self._playlists:
            return "No playlists saved yet. Ask me to add one with a name, music query, and optional moods."
        parts = []
        for name, entry in self._playlists.items():
            moods = ", ".join(entry["moods"])
            parts.append(f"{name} ({moods or entry['query']})")
        return "Saved playlists: " + "; ".join(parts) + "."

    def choose_playlist(self, request: str = "", hour: int | None = None) -> tuple[str, dict[str, Any]]:
        """Pick the best saved playlist for a request.

        Three things score, in this order of weight: a word from the request
        that matches the playlist's own mood tags (the user's stated intent,
        three points), a word that matches anywhere in its name or query (one
        point), and finally the hour. The hour only ever decides when *nothing*
        in the request matched any playlist — so "something energetic" still
        wins at midnight, while a bare "choose a playlist" is answered by what
        suits the time of day.
        """
        if not self._playlists:
            raise RuntimeError("No playlists are saved yet. Add one first.")
        words = set(re.findall(r"[a-z0-9]+", request.casefold()))
        now_moods = set(preferred_moods(datetime.now().hour if hour is None else hour))
        scored: list[tuple[int, str, dict[str, Any], set[str]]] = []
        anything_matched = False
        for name, entry in self._playlists.items():
            labels = set(re.findall(
                r"[a-z0-9]+", " ".join([name, entry["query"], *entry["moods"]]).casefold()))
            # Mood tags are intentional preferences, so they outweigh an
            # incidental match in a search query.
            mood_words = set(re.findall(r"[a-z0-9]+", " ".join(entry["moods"]).casefold()))
            score = (len(words_matching(words, labels))
                     + 3 * len(words_matching(words, mood_words)))
            anything_matched = anything_matched or score > 0
            scored.append((score, name, entry, mood_words))
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for score, name, entry, mood_words in scored:
            if not anything_matched and mood_words & now_moods:
                # The request named nothing any playlist could claim, so the
                # hour may break the tie — it never outranks a real match.
                score += 2
            candidates.append((score, name, entry))
        best = max(score for score, _, _ in candidates)
        tied = [(name, entry) for score, name, entry in candidates if score == best]
        selected = tied[self._playlist_cursor % len(tied)]
        self._playlist_cursor += 1
        name, entry = selected
        asked_for = bool(words_matching(words, set(re.findall(
            r"[a-z0-9]+", " ".join([name, entry["query"], *entry["moods"]]).casefold()))))
        self.last_choice_reason = ("it matched what you asked for" if asked_for
                                   else f"it suits the {hour_label(datetime.now().hour if hour is None else hour)}")
        return selected

    def suspend_for_voice(self) -> None:
        self._voice_depth += 1
        self._voice_clear.clear()

    def resume_after_voice(self) -> None:
        self._voice_depth = max(0, self._voice_depth - 1)
        if self._voice_depth == 0:
            self._voice_clear.set()

    async def wait_for_voice(self) -> None:
        """Finish at most one 100-ms music write, then discard remote queued music."""
        async with self._output_lock:
            if self._voice_depth == 1 and self.task is not None and not self.task.done():
                await self.speaker.stop()

    async def _play_music_chunk(self, pcm: bytes, rate: int, channels: int,
                                generation: int, describes_itself: bool = True) -> bool:
        while True:
            await self._voice_clear.wait()
            await self._playback_clear.wait()
            async with self._output_lock:
                if generation != self._track_generation:
                    return False
                # A pause/voice request can arrive while a packet awaits the lock.
                if not self._voice_clear.is_set() or not self._playback_clear.is_set():
                    continue
                if describes_itself:
                    await self.speaker.play(pcm, rate, channels)
                else:
                    await self.speaker.play(pcm)
                return True

    def set_volume(self, percent: int) -> None:
        self.volume = max(0.0, min(1.0, int(percent) / 100.0))
        self._volume_table = None

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
        table = self._volume_table
        if table is None:
            # One 65536-entry table per level: entry[u] is the
            # two's-complement bit pattern of int(sample * volume), with u the
            # unsigned reading of that sample — identical results to the
            # per-sample loop it replaces, at lookup speed.
            volume = self.volume
            table = [int((u if u < 32768 else u - 65536) * volume) % 65536
                     for u in range(65536)]
            self._volume_table = table
        samples = array("H")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        scaled = array("H", map(table.__getitem__, samples))
        if sys.byteorder != "little":
            scaled.byteswap()
        return scaled.tobytes()

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

    def _music_format(self) -> tuple[int, int]:
        """The rate and channel count music should be decoded to.

        Whatever is carrying the sound decides: the board's own PortAudio
        stream is mono, while a browser can take full-rate stereo. Asking the
        speaker keeps the decoder and the output from disagreeing, which would
        show up as the wrong pitch or half a track.
        """
        format_of = getattr(self.speaker, "music_format", None)
        if format_of is None:
            return 24_000, 1
        rate, channels = format_of
        return int(rate), int(channels)

    async def _stream_url(self, song_id: str) -> str:
        endpoint = "https://music.163.com/api/song/enhance/player/url/v1"
        try:
            async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as client:
                response = await client.get(endpoint, params={
                    "ids": json.dumps([int(song_id)]), "level": self.level, "encodeType": "mp3"
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
            if self.speaker is None:
                raise RuntimeError("No ATHENA audio output is available.")
            if getattr(self.speaker, "available", True) is False:
                raise RuntimeError("No computer browser is connected for audio. Open the dashboard and press Start.")
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
            rate, channels = self._music_format()
            # Only a speaker that advertises a music format can be told one;
            # anything else keeps the single-argument call it was written for.
            describes_itself = getattr(self.speaker, "music_format", None) is not None
            # A tenth of a second per packet, whatever the format works out to.
            chunk_bytes = max(2, rate * channels * 2 // 10)
            while self.queue:
                song_id, title = self.queue.pop(0)
                stream_url = await self._stream_url(song_id)
                self.current_title = title
                # `-vn` keeps embedded cover art from being handed to a raw PCM
                # output, and the rate and channel count come from the speaker
                # rather than from a constant.
                self.process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-loglevel", "error", "-i", stream_url, "-vn",
                    "-f", "s16le", "-acodec", "pcm_s16le",
                    "-ac", str(channels), "-ar", str(rate), "pipe:1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                self.paused = False
                self._playback_clear.set()
                generation = self._track_generation
                pending = bytearray()
                while self.process.stdout is not None:
                    chunk = await self.process.stdout.read(chunk_bytes)
                    if not chunk:
                        break
                    pending.extend(chunk)
                    aligned = len(pending) // (channels * 2) * (channels * 2)
                    if not aligned:
                        continue
                    chunk = bytes(pending[:aligned])
                    del pending[:aligned]
                    self._started.set()
                    pcm = self._apply_volume(chunk)
                    if not await self._play_music_chunk(pcm, rate, channels, generation, describes_itself):
                        break
                await self.process.communicate()
                self.process = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = f"NetEase playback failed: {error}"
            self._started.set()
            print(f"NetEase playback failed: {error}", flush=True)
        finally:
            process, self.process = self.process, None
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                # Drain pipes after killing a backpressured decoder. wait()
                # alone can hang while unread stdout keeps the transport open.
                await process.communicate()
            self.paused = False
            if not self._started.is_set() and not self.last_error:
                self.last_error = "NetEase returned no playable audio for those results."
            self._started.set()

    async def pause(self) -> None:
        if self.process is None:
            raise RuntimeError("No NetEase track is playing.")
        self.paused = True
        self._playback_clear.clear()
        async with self._output_lock:
            if self._voice_depth == 0:
                await self.speaker.stop()

    async def resume(self) -> None:
        if self.process is None:
            raise RuntimeError("No NetEase track is playing.")
        if self.paused:
            self.paused = False
            self._playback_clear.set()

    async def next(self) -> None:
        if self.process is None and not self.queue:
            raise RuntimeError("No NetEase track is playing.")
        if self.process is not None:
            self._track_generation += 1
            self.process.kill()
        self.paused = False
        self._playback_clear.set()

    async def stop(self) -> None:
        self._track_generation += 1
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        if self.process is not None and self.process.returncode is None:
            self.process.kill(); await self.process.communicate()
        self.process = None; self.queue.clear(); self.current_title = ""; self.paused = False
        self._playback_clear.set()
        async with self._output_lock:
            if self._voice_depth == 0 and self.speaker is not None:
                await self.speaker.stop()


class NetEaseMusicTool:
    definition = ToolDefinition(
        name="netease_music",
        description=("Play music from NetEase Cloud Music. Add named playlists with a search query and mood "
                     "tags, then auto_play chooses the best saved playlist for the request — weighing the "
                     "mood tags against what was asked for, and favouring tags that suit the time of day "
                     "when nothing was specified. Playback runs in the background and uses the default "
                     "audio device of whichever computer is connected in browser-audio mode."),
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["play", "pause", "resume", "next", "stop", "add_playlist", "remove_playlist", "list_playlists", "play_playlist", "auto_play"]},
            "query": {"type": "string"},
            "name": {"type": "string"},
            "moods": {"type": "array", "items": {"type": "string"}}},
            "required": ["action"], "additionalProperties": False},
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
            if action == "add_playlist":
                name = str(arguments.get("name", ""))
                query = str(arguments.get("query", ""))
                moods = arguments.get("moods", [])
                if not isinstance(moods, list):
                    return ToolResult(False, "Playlist moods must be a list of short tags.")
                saved = self.player.add_playlist(name, query, moods)
                return ToolResult(True, f"Saved the {saved} playlist. I can choose it automatically when it fits.")
            if action == "remove_playlist":
                removed = self.player.remove_playlist(str(arguments.get("name", "")))
                return ToolResult(True, f"Removed the {removed} playlist.")
            if action == "list_playlists":
                return ToolResult(True, self.player.playlist_summary())
            if action in {"play_playlist", "auto_play"}:
                reason = ""
                if action == "play_playlist":
                    found = self.player._named_playlist(str(arguments.get("name", "")))
                    if found is None:
                        return ToolResult(False, "I don't have that playlist. Ask me to list your saved playlists.")
                    name, entry = found
                else:
                    name, entry = self.player.choose_playlist(str(arguments.get("query", "")))
                    if self.player.last_choice_reason:
                        reason = f" ({self.player.last_choice_reason})"
                await self.player.play(entry["query"])
                return ToolResult(True, f"Playing your {name} playlist.")
            if action == "play":
                query = str(arguments.get("query", "")).strip()
                if not query or len(query) > 300:
                    return ToolResult(False, "Tell me which song or artist to play.")
                await self.player.play(query)
                title = self.player.current_title or (self.player.queue[0][1] if self.player.queue else query)
                return ToolResult(True, f"Playing {title}.")
            if action == "pause":
                await self.player.pause(); return ToolResult(True, "Paused.")
            if action == "resume":
                await self.player.resume(); return ToolResult(True, "Resuming.")
            if action == "next":
                await self.player.next(); return ToolResult(True, "Skipping to the next track.")
            await self.player.stop(); return ToolResult(True, "Music stopped.")
        except (RuntimeError, OSError) as error:
            return ToolResult(False, str(error))


TOOL = NetEaseMusicTool()
