"""Speech through Microsoft Edge's online voices: free, and far better than local.

Edge exposes the same neural voices its browser uses, without an account, an API
key or a bill. That makes it the cheapest good voice available — free where the
cloud voice is billed per character and the local voices sound synthetic.

**It is not an official API.** `edge-tts` speaks the Edge browser's own protocol,
reverse-engineered, so Microsoft can change or close it without notice. That is the
whole reason this backend reports its failures loudly and the fallback to a local
voice exists: an unofficial service is one that can stop working on a Tuesday, and
a voice that silently goes quiet is worse than one that says why.

The transport needs two hops, which is why this module is longer than the others:

    edge-tts --MP3--> ffmpeg --PCM--> the speaker

Edge only emits MP3, and the audio pipeline wants raw PCM. `ffmpeg` does that
conversion as a *stream*, so audio starts playing while the rest is still
arriving rather than after the whole reply is decoded.

Latency on the Orange Pi, measured 2026-09-21 (en-GB-RyanNeural, one clause):

    Edge websocket connect + first MP3 byte    ~1170 ms
    ffmpeg process spawn                        ~680 ms
    first PCM through the full synthesizer     ~1230 ms

Every one of those used to sit on the critical path *per clause*, and the
coordinator used to wait for each clause's synthesis before reading more of the
model's reply — a three-clause answer stalled for four seconds in synthesis and
audibly broke apart between clauses. So the synthesizer now runs a per-turn
pump: `send_text` only enqueues and returns at once, the model keeps streaming,
clause N+1's Edge connection is paid while clause N is still being heard, and
one decoder carries every clause of the turn (MP3 is a frame stream; successive
clauses decode continuously through the same process). The spawn itself is paid
in parallel with the first Edge connection, and `connect()` warms the route so
the first reply of the day doesn't also pay for a cold DNS resolver.

The voices are 24 kHz mono, so the speaker is opened at 24000 — the same rate the
cloud voice used, and a different one from the local VITS voice at 22050.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from uuid import UUID

from athena.events import AudioChunk

# Edge's neural voices are 24 kHz mono, matching the cloud voice's rate.
SAMPLE_RATE = 24_000

# Ava is Edge's most expressive English voice. The user explicitly prefers
# emotion; its slightly slower first byte is offset by keeping clauses longer
# and avoiding an Edge connection at every comma.
DEFAULT_VOICE = "en-US-AvaNeural"

# How much decoded PCM to hand over at a time, matching the other backends so the
# speaker sees the same cadence regardless of which voice is configured.
CHUNK_BYTES = 8_192

# Synthesis is bounded so a wedged ffmpeg costs one reply rather than the service.
# Generous, because a long briefing is genuinely a lot of audio.
SYNTHESIS_TIMEOUT_SECONDS = 120.0
# The whole reply must arrive within this or the turn ends silently.
STREAM_TIMEOUT_SECONDS = 60.0


def edge_voice() -> str:
    return os.environ.get("ATHENA_EDGE_VOICE", "").strip() or DEFAULT_VOICE


def edge_rate() -> str:
    """Speaking rate as Edge spells it, e.g. '+10%' or '-5%'. Empty by default.

    Passed through rather than parsed: Edge has its own idea of valid values and
    rejects what it dislikes clearly enough to diagnose from the log.
    """
    return os.environ.get("ATHENA_EDGE_RATE", "").strip()


def edge_pitch() -> str:
    """Pitch shift, e.g. '+10Hz'. Empty by default, and that default is load-bearing.

    Sending a no-op `pitch` costs real latency: measured on the board, `+0Hz` moved
    time-to-first-audio from ~0.85 s to 1.2-2.0 s, while `rate=+0%` alone stayed at
    0.85 s. The prosody path is evidently not free, so nothing is sent unless it was
    actually asked for.
    """
    return os.environ.get("ATHENA_EDGE_PITCH", "").strip()


def edge_sample_rate() -> int:
    try:
        return int(os.environ.get("ATHENA_EDGE_SAMPLE_RATE", str(SAMPLE_RATE)))
    except ValueError:
        return SAMPLE_RATE


def decoder_command(sample_rate: int | None = None) -> list[str]:
    """The ffmpeg invocation that turns the MP3 stream into raw PCM.

    `-loglevel error` keeps ffmpeg's banner out of the service log; its output is
    piped, so anything chatty lands in the middle of the audio path's diagnostics.
    """
    rate = sample_rate or edge_sample_rate()
    return [
        decoder_binary() or "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "mp3", "-probesize", "32", "-analyzeduration", "0",
        "-i", "pipe:0",
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(rate), "-ac", "1",
        "pipe:1",
    ]


def decoder_binary() -> str | None:
    executable = shutil.which('ffmpeg')
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return None


def edge_available() -> tuple[bool, str]:
    """Whether this voice can run here, and why not when it cannot.

    The two dependencies are reported separately because their fixes are
    different: one is `pip install edge-tts`, the other is `apt install ffmpeg`.
    """
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        return False, "edge-tts is not installed (pip install edge-tts)"
    if decoder_binary() is None:
        return False, "ffmpeg is not installed, and Edge returns MP3 (install ffmpeg or imageio-ffmpeg)"
    return True, ""


class EdgeSynthesizer:
    """Streams a reply through Edge's voices and ffmpeg into the speaker.

    Synthesis runs on a per-turn pump so speaking is never serialized behind
    it: `send_text` only enqueues the clause and returns at once, the model
    keeps streaming while Edge connects, and clause N+1's connection is paid
    while clause N is still being heard. One decoder carries every clause of
    the turn — MP3 is a frame stream, so successive clauses decode
    continuously through the same process — and it is spawned in parallel with
    the first Edge connection.

    The turn protocol matches the other backends: `send_text` publishes PCM under
    a turn id, `flush` publishes the turn sentinel, and `audio` yields until it
    sees that sentinel. The sentinel is published on every path, including
    failure, because a turn that never ends is a service that stops speaking.
    """

    def __init__(self, voice: str | None = None, rate: str | None = None,
                 pitch: str | None = None, settings=None,
                 sample_rate: int | None = None) -> None:
        selected = settings.get('edge_voice') if settings is not None else 'system'
        self._voice = voice or (selected if selected != 'system' else edge_voice())
        self._rate = rate or edge_rate()
        self._pitch = pitch or edge_pitch()
        self._settings = settings
        self.sample_rate = sample_rate or edge_sample_rate()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._audio: asyncio.Queue[AudioChunk | UUID] = asyncio.Queue()
        self._turn_id: UUID | None = None
        # The ffmpeg process for the turn in flight, so `cancel` can end it.
        self._process: asyncio.subprocess.Process | None = None
        # Pending clauses for the turn in flight; None is the end-of-turn marker.
        self._pending: asyncio.Queue[str | None] = asyncio.Queue()
        self._pump_task: asyncio.Task | None = None
        # Decoder diagnostics: what ffmpeg said, for when a turn comes out silent.
        self._stderr_task: asyncio.Task | None = None
        self._last_exit: int | None = None
        self._last_stderr: str = ""
        self.last_first_byte_ms = 0.0
        self.last_total_ms = 0.0

    async def connect(self) -> None:
        """Record the loop, and warm the route to Edge.

        The network is otherwise only touched per reply. A board that has sat
        idle for hours pays its worst DNS, ARP and TLS setup on the very first
        reply; one throwaway connection at start-up takes that off the turn
        path. Best effort by design — failing here would fail the first
        synthesis anyway, with a better error message.
        """
        self._loop = asyncio.get_running_loop()
        available, reason = edge_available()
        if not available:
            raise RuntimeError(f"Edge speech is not usable: {reason}")
        try:
            import ssl
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("speech.platform.bing.com", 443,
                                        ssl=ssl.create_default_context()),
                timeout=5.0)
            writer.close()
        except Exception:
            pass

    async def send_text(self, turn_id: UUID, text: str) -> None:
        """Enqueue one clause and return before any synthesis has happened.

        Blocking here would stall the model's stream behind the network — the
        exact mistake that made multi-clause answers fall apart between
        clauses. The pump owns the waiting.
        """
        if not text or not text.strip():
            return
        if self._turn_id is not None and self._turn_id != turn_id:
            # A newer reply supersedes the old one; end it so its playback stops.
            self._publish(self._turn_id)
            await self._stop_pump()
        self._turn_id = turn_id
        if self._pump_task is None:
            self._pending = asyncio.Queue()
            self._pump_task = asyncio.create_task(self._pump(turn_id))
        self._pending.put_nowait(text)

    async def _pump(self, turn_id: UUID) -> None:
        """Synthesize the turn's clauses through one decoder, PCM to the queue.

        The clause loop and the decoder drain live in this one task: the drain
        is created once, at the first MP3 byte of the turn, and is the only
        reader of the decoder's stdout. Per-clause state therefore cannot
        outlive its clause, and the drain is always awaited by whoever owns it.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        first_ms: float | None = None
        drain_task: asyncio.Task | None = None
        spawn: asyncio.Task | None = None
        try:
            # The decoder is paid in parallel with the first Edge connection:
            # on this board the spawn alone is ~0.7 s of the first-audio path.
            spawn = asyncio.create_task(self._spawn_decoder())
            while True:
                text = await self._pending.get()
                if text is None:
                    break
                async with asyncio.timeout(SYNTHESIS_TIMEOUT_SECONDS):
                    import edge_tts

                    # Only send prosody that was actually configured. A no-op
                    # `pitch` is not free — see `edge_pitch` — so an unset
                    # value must stay unset.
                    options: dict[str, str] = {}
                    if self._rate:
                        options["rate"] = self._rate
                    if self._pitch:
                        options["pitch"] = self._pitch
                    communicate = edge_tts.Communicate(text, self._voice,
                                                       **options)
                    async for chunk in communicate.stream():
                        if chunk.get("type") != "audio":
                            continue
                        data = chunk.get("data") or b""
                        if not data:
                            continue
                        if drain_task is None:
                            # First audio of the turn: resolve the spawn (paid
                            # in parallel with the Edge connect, so by here it
                            # is already done) and start the drain, so the
                            # pipe never fills. The gate is the drain task,
                            # not the process: the spawn usually completes
                            # long before the first MP3 byte, and a check on
                            # `_process` would then never fire.
                            process = await spawn
                            drain_task = asyncio.create_task(
                                self._drain_decoder(turn_id, process,
                                                    started))
                        stdin = self._process.stdin
                        if stdin is None or stdin.is_closing():
                            continue
                        stdin.write(data)
                        await stdin.drain()
                if self._turn_id != turn_id:
                    return
            if drain_task is not None:
                self._close_decoder_input()
                first_ms = await drain_task
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Never leave the turn open, and never pretend it worked. An
            # unofficial service fails in ways worth naming.
            print(f"[speech] Edge synthesis failed: "
                  f"{type(error).__name__}: {error}", flush=True)
            await self._report_decoder()
            self._publish(turn_id)
        else:
            if first_ms is None and drain_task is not None:
                # Edge reported success but nothing decoded. Silence with no
                # reason attached is the one failure mode worth extra work.
                await self._report_decoder()
            if self._turn_id == turn_id:
                self._publish(turn_id)
        finally:
            if spawn is not None and not spawn.done():
                spawn.cancel()
            if drain_task is not None and not drain_task.done():
                # The success path already awaited the drain; this only fires
                # on failure or supersede, where the remaining audio is moot.
                drain_task.cancel()
            if self._stderr_task is not None and not self._stderr_task.done():
                self._stderr_task.cancel()
            await asyncio.gather(*(task for task in (spawn, drain_task, self._stderr_task)
                                   if task is not None), return_exceptions=True)
            if first_ms is not None:
                self.last_first_byte_ms = first_ms
            self.last_total_ms = (loop.time() - started) * 1000
            await self._kill_process()
            if self._pump_task is asyncio.current_task():
                self._pump_task = None

    async def _spawn_decoder(self):
        process = await asyncio.create_subprocess_exec(
            *decoder_command(self.sample_rate),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._process = process
        self._last_exit = None
        self._last_stderr = ""
        # Drained from the start: a decoder that fills its stderr pipe stalls
        # at exactly the moment it is trying to say why it failed.
        self._stderr_task = asyncio.create_task(self._read_stderr(process))
        return process

    async def _read_stderr(self, process) -> None:
        """Accumulate the decoder's stderr, so a silent turn can be explained."""
        chunks: list[bytes] = []
        try:
            while True:
                data = await process.stderr.read(4_096)
                if not data:
                    break
                chunks.append(data)
        except Exception:
            pass
        self._last_exit = process.returncode
        text = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if text:
            self._last_stderr = text

    async def _report_decoder(self) -> None:
        """Print what the decoder had to say, when it had something to say."""
        task = self._stderr_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except Exception:
                pass
        bits: list[str] = []
        if self._last_stderr:
            bits.append(f"stderr: {self._last_stderr}")
        if self._last_exit is not None and self._last_exit != 0:
            bits.append(f"exit code {self._last_exit}")
        if bits:
            print(f"[speech] ffmpeg decoder: {'; '.join(bits)}", flush=True)

    async def _drain_decoder(self, turn_id: UUID, process,
                             started: float) -> float | None:
        """Publish decoded PCM until the decoder's output ends.

        Runs alongside synthesis, so the pipe never fills and playback starts
        on the first decoded bytes rather than after the clause is decoded.
        `started` is the pump's start time, so the timing measures the whole
        turn's time-to-first-audio rather than the drain's.
        """
        loop = asyncio.get_running_loop()
        first: float | None = None
        remainder = b""
        while True:
            pcm = await process.stdout.read(CHUNK_BYTES)
            if not pcm:
                break
            pcm = remainder + pcm
            aligned = len(pcm) - len(pcm) % 2
            pcm, remainder = pcm[:aligned], pcm[aligned:]
            if not pcm:
                continue
            if self._turn_id != turn_id:
                # Superseded mid-reply: stop producing audio nobody will hear.
                break
            if first is None:
                first = (loop.time() - started) * 1000
                self.last_first_byte_ms = first
            self._publish(AudioChunk(turn_id, pcm))
        return first

    def _close_decoder_input(self) -> None:
        stdin = self._process.stdin if self._process is not None else None
        if stdin is not None and not stdin.is_closing():
            stdin.close()

    async def _stop_pump(self) -> None:
        task, self._pump_task = self._pump_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._kill_process()

    async def _kill_process(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (TimeoutError, ProcessLookupError):
            pass

    async def audio(self, turn_id: UUID | None = None):
        """Yield this turn's PCM until its sentinel arrives."""
        wanted = turn_id or self._turn_id
        while True:
            item = await self._audio.get()
            if isinstance(item, UUID):
                if item == wanted:
                    return
                continue
            if item.turn_id == wanted:
                yield item

    async def flush(self, turn_id: UUID) -> None:
        if self._pump_task is not None and self._turn_id == turn_id:
            self._pending.put_nowait(None)
        else:
            self._publish(turn_id)

    async def cancel(self, turn_id: UUID) -> None:
        if self._turn_id == turn_id:
            self._turn_id = None
        await self._stop_pump()
        self._publish(turn_id)

    async def close(self) -> None:
        await self._stop_pump()

    async def cache_phrase(self, text: str) -> bytes:
        """Synthesize without playing, so a fixed phrase can be replayed free."""
        from uuid import uuid4
        turn = uuid4()
        collected: list[bytes] = []

        async def drain() -> None:
            async for chunk in self.audio(turn):
                collected.append(chunk.pcm)

        reader = asyncio.create_task(drain())
        try:
            await self.send_text(turn, text)
            await self.flush(turn)
            await asyncio.wait_for(reader, timeout=STREAM_TIMEOUT_SECONDS)
        finally:
            reader.cancel()
        return b"".join(collected)

    def _publish(self, item: AudioChunk | UUID) -> None:
        """Hand an item to the consumer, from whatever task produced it."""
        if self._loop is None or self._loop.is_closed():
            return

        def put() -> None:
            self._audio.put_nowait(item)

        self._loop.call_soon_threadsafe(put)
