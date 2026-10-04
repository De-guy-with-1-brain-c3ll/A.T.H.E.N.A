"""Local speech with Piper: no network, no API key, no per-character cost.

Piper runs on CPU-only ARM at roughly eight times realtime, so an Orange Pi can
synthesize speech faster than it plays. That makes it the right default for heavy
use: the cloud voice costs ¥1 per 10,000 characters, and every TTS on that
platform costs the same, so the only way to stop paying by the word is to stop
sending the words anywhere.

The trade is voice quality. Piper is clearly synthetic — clean and intelligible,
not as natural as the cloud voice. `ATHENA_TTS_BACKEND=qwen` keeps the cloud
voice for when that matters.

The interesting cost is not synthesis, it is start-up. Measured on this machine,
starting Piper and loading a medium voice takes ~1.83s while synthesizing a whole
sentence adds almost nothing, so a fresh process per reply spends nearly two
seconds loading a model it is about to throw away. `send_text` therefore keeps
the process alive for the lifetime of the synthesizer and reuses it: the first
reply of a session pays the load, every reply after it starts speaking
immediately.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import unicodedata
from uuid import UUID

from athena.events import AudioChunk


# Piper's medium English voices are 22.05 kHz mono 16-bit. The speaker is opened
# at this rate, so no resampling is needed — and resampling speech is exactly the
# kind of thing that quietly degrades quality.
DEFAULT_SAMPLE_RATE = 22_050
DEFAULT_VOICE = "en_US-lessac-medium"
READ_CHUNK_BYTES = 8_192
# How long the reader must see no queued audio before a reply counts as finished.
# Long enough to ride out the gap between Piper's write chunks, short enough that
# it is not audible as a pause before playback is declared over.
QUIET_SETTLE_SECONDS = 0.15
# Thrown away by warm(): short enough to cost nothing, real enough that Piper
# produces audio whose end marks the voice as loaded.
WARM_PHRASE = b"Hi.\n"
# A quiet poll after warm-up audio means Piper has finished the tiny "Hi."
# phrase.  The old 600 ms was longer than the phrase itself and put an
# avoidable half-second on readiness during service boot.  200 ms still drains
# the phrase before the live reader takes ownership, without delaying the first
# real reply behind an idle sleep.
SWALLOW_POLL_SECONDS = 0.2


def voices_directory() -> Path:
    configured = os.environ.get("ATHENA_PIPER_VOICES", "").strip()
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "piper-voices"


def piper_binary() -> str:
    return os.environ.get("ATHENA_PIPER_BINARY", "").strip() or "piper"


def piper_voice() -> str:
    return os.environ.get("ATHENA_PIPER_VOICE", "").strip() or DEFAULT_VOICE


def piper_sample_rate() -> int:
    try:
        return int(os.environ.get("ATHENA_PIPER_SAMPLE_RATE", str(DEFAULT_SAMPLE_RATE)))
    except ValueError:
        return DEFAULT_SAMPLE_RATE


def piper_idle_seconds() -> float:
    """How long an unused Piper process is kept loaded.

    A warm voice is the difference between a quick reply and a cold model load.
    The default therefore keeps it for the service lifetime. Set a positive
    value only when reclaiming the model's memory matters more than the first
    reply after a pause; zero disables retirement.
    """
    try:
        return max(0.0, float(os.environ.get("ATHENA_PIPER_IDLE_SECONDS", "0")))
    except ValueError:
        return 0.0


def piper_turn_timeout_seconds() -> float:
    """How long to wait for a reply's audio before ending the turn without it."""
    try:
        return max(1.0, float(os.environ.get("ATHENA_PIPER_TURN_TIMEOUT", "60")))
    except ValueError:
        return 60.0


def piper_warm_timeout_seconds() -> float:
    """How long the warm-up will spend loading a voice before giving up."""
    try:
        return max(0.5, float(os.environ.get("ATHENA_PIPER_WARM_TIMEOUT", "30")))
    except ValueError:
        return 30.0


def piper_environment() -> dict[str, str]:
    """The environment Piper must be started with, and it is not optional.

    Piper's CLI reads its input with `for line in sys.stdin`. That iterates a
    Python text stream, which reads *ahead* into an internal buffer, so the
    process sits on the next sentence instead of synthesizing what it already
    has: audio for one sentence appears and then the stream stalls with the
    process healthy and no EOF. The visible symptom is a warm process that
    answers the first reply and then goes silent forever — and closing stdin to
    shake it loose destroys the process this whole design is built to reuse.

    Unbuffered mode removes the read-ahead, restoring one line in, one sentence
    out. Without this the reused process is strictly worse than a fresh one.
    """
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def piper_input_text(text: str) -> str:
    """Make streamed assistant prose safe for Piper's Windows CLI parser.

    The installed Piper build can silently produce no audio for a spaced em dash
    (``"Got it — CJ"``), even though the same words with ASCII punctuation work.
    Models regularly emit typographic punctuation, so convert those presentation
    characters before sending a line. This is intentionally limited to English
    punctuation; it does not strip ordinary non-ASCII words from another voice.
    """
    replacements = str.maketrans({
        "—": "-", "–": "-", "−": "-",
        "“": '"', "”": '"', "„": '"',
        "‘": "'", "’": "'", "…": "...",
        "\u00a0": " ",
    })
    # NFKC also turns full-width ASCII punctuation into the form Piper expects.
    return unicodedata.normalize("NFKC", text).translate(replacements)


def find_voice(name: str) -> Path | None:
    """Locate a voice model, accepting either a full path or a bare name."""
    direct = Path(name)
    if direct.suffix == ".onnx" and direct.is_file():
        return direct
    folder = voices_directory()
    for candidate in (folder / f"{name}.onnx", folder / name / f"{name}.onnx"):
        if candidate.is_file():
            return candidate
    # Only search by bare name. A name that still looks like a path would be an
    # absolute pattern, which rglob refuses, and a missing voice is a configuration
    # mistake the caller reports — it must not be a crash in the constructor.
    if "/" not in name and "\\" not in name and folder.is_dir():
        matches = sorted(folder.rglob(f"{name}.onnx"))
        if matches:
            return matches[0]
    return None


def binary_problem(binary: str) -> str | None:
    """Why this Piper binary cannot be run, or None when it can."""
    # An explicit path is taken at its word; only a bare name is searched for.
    if os.sep in binary or "/" in binary:
        if not Path(binary).is_file():
            return f"the piper binary ({binary}) is not there"
        return None
    if shutil.which(binary) is None:
        return f"the piper binary ({binary}) is not installed"
    return None


def piper_available() -> tuple[bool, str]:
    """Whether local speech can run here with the configured settings."""
    problem = binary_problem(piper_binary())
    if problem:
        return False, problem
    if find_voice(piper_voice()) is None:
        return False, f"the voice {piper_voice()!r} is not in {voices_directory()}"
    return True, ""


class PiperSynthesizer:
    """Streams raw PCM from a local Piper process.

    Same interface as the cloud synthesizer, so the coordinator is unchanged.

    The Piper process is started once and then fed a line per clause until it
    goes idle only when configured (`ATHENA_PIPER_IDLE_SECONDS`, default 0), which is the point of
    the design: Piper reads sentences as a stream, so one process can answer many
    turns and the ~1.8s voice load is paid once rather than per reply.
    """

    sample_rate = DEFAULT_SAMPLE_RATE

    def __init__(self, voice: str | None = None, binary: str | None = None,
                 sample_rate: int | None = None, settings=None,
                 prefix_args: list[str] | None = None,
                 idle_seconds: float | None = None) -> None:
        self._voice_name = voice or piper_voice()
        self._binary = binary or piper_binary()
        # Lets Piper be reached through a wrapper, and lets the tests drive a
        # stand-in without needing a real ARM binary on the build machine.
        self._prefix_args = list(prefix_args or [])
        self.sample_rate = sample_rate or piper_sample_rate()
        self._settings = settings
        self._idle_seconds = piper_idle_seconds() if idle_seconds is None else idle_seconds
        # When a turn ends there is nothing left to read, so a line means
        # "done". No idle window runs during a turn — a slow speaker must never
        # lose the middle of a reply to a timer.
        self._turn_timeout = piper_turn_timeout_seconds()
        self._warm_timeout = piper_warm_timeout_seconds()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._audio: asyncio.Queue[AudioChunk | UUID] = asyncio.Queue()
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        # `warm()` starts at service boot while the first user utterance may
        # already be in flight.  Without a lock, `send_text()` sees no live
        # process while warm-up owns an unassigned local process and starts a
        # second Piper.  Besides doubling the peak memory, that makes the
        # first spoken answer compete with model loading on the small CPU.
        self._process_lock = asyncio.Lock()
        self._turn_id: UUID | None = None
        self._voice_path = find_voice(self._voice_name)
        # `_pump` is the only stdout reader.  Completion must observe it rather
        # than StreamReader's private buffer: by the time `flush` inspects that
        # buffer, the pump has correctly drained it and a healthy turn looks
        # like it never produced audio.
        self._turn_saw_audio = False
        self._turn_last_audio_at = 0.0

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Check this instance, not the environment: a caller may have passed an
        # explicit voice or binary, and the tests always do.
        problem = binary_problem(self._binary)
        if problem:
            raise RuntimeError(f"Piper is not usable: {problem}")
        if self._voice_path is None:
            raise RuntimeError(
                f"Piper is not usable: the voice {self._voice_name!r} "
                f"is not in {voices_directory()}")

    async def cache_phrase(self, text: str) -> bytes:
        """Synthesize without playing, used to pre-render the fixed phrases."""
        from uuid import uuid4
        cache = PiperSynthesizer(self._voice_name, self._binary, self.sample_rate,
                                 self._settings)
        turn = uuid4()
        try:
            await cache.connect()
            await cache.send_text(turn, text)
            await cache.flush(turn)
            return b"".join([chunk.pcm async for chunk in cache.audio(turn)])
        finally:
            await cache.close()

    async def warm(self) -> bool:
        """Load the voice now so the first reply does not wait for it.

        Spawning the process is not enough, and this is the subtlety worth
        keeping: Piper takes ~1.8s to load a voice, but starting the executable
        takes 8ms because the load happens lazily on the first line it reads.
        A warm-up that only spawns therefore returns immediately and buys
        nothing — the first real reply still waits. The voice is loaded by
        actually synthesizing something and throwing the audio away.

        The process is started *after* the warm-up phrase is done, so the pump
        only ever reads audio belonging to a real turn. Starting it first would
        let the pump hand the warm-up audio to whatever turn came next, which is
        both wrong audio and, once discarded, no audio at all.

        Best-effort by design: a failure here costs the next reply its head
        start, which is what would have happened anyway, so it must not be able
        to break a turn that is still perfectly able to speak.
        """
        async with self._process_lock:
            if self._voice_path is None:
                return False
            process = None
            try:
                if self._process is not None and self._process.returncode is None:
                    return True  # already warm
                process = await self._spawn()
                if process.stdin is None or process.stdout is None:
                    await self._stop(process)
                    return False
                # A real word, not a space: Piper synthesizes nothing for whitespace,
                # so a blank phrase loads the voice but produces no audio to detect
                # the end of the load by. "Hi." is one sentence and a fraction of a
                # second of audio.
                process.stdin.write(WARM_PHRASE)
                await process.stdin.drain()
                await asyncio.wait_for(self._swallow_audio(process), timeout=self._warm_timeout)
                # Hand the process on as the live one and start its single reader.
                self._process = process
                self._task = asyncio.create_task(self._pump())
                return True
            except asyncio.CancelledError:
                # `connect()` deliberately warms in the background.  A service
                # shutdown can cancel that task while the subprocess is still
                # local to this method; without explicit cleanup it survives
                # as an orphan and competes with the next service start.
                # The cancellation that brought us here remains pending and
                # would otherwise interrupt `_stop()` at its `wait()`. Clear
                # that one handled request, reap the child, then re-raise the
                # same cancellation to preserve normal shutdown semantics.
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                await self._stop(process)
                raise
            except Exception:
                await self._stop(process)
                self._process = None
                self._task = None
                return False

    async def _swallow_audio(self, process: asyncio.subprocess.Process) -> None:
        """Read and discard audio until it stops coming, which ends the load.

        Audio must be *seen* before its absence means anything. Piper is silent
        for the whole of the model load — most of two seconds — so a quiet poll
        on its own returns before the voice is ready and the warm-up buys
        nothing while appearing to succeed. Nothing else is reading the stream
        while this runs, so the pause after the warm-up sentence is observed
        directly rather than inferred from a shared buffer.
        """
        saw_audio = False
        while True:
            if process.returncode is not None or process.stdout is None:
                return
            try:
                block = await asyncio.wait_for(
                    process.stdout.read(READ_CHUNK_BYTES), timeout=SWALLOW_POLL_SECONDS)
            except asyncio.TimeoutError:
                if saw_audio:
                    return  # the warm-up sentence has played out
                continue  # still loading; keep waiting
            if not block:
                return  # Piper closed the stream
            saw_audio = True
            # The audio exists only to force the load; it is deliberately dropped.

    async def _spawn(self) -> asyncio.subprocess.Process:
        """Start a Piper process. The caller decides when it goes into service."""
        if self._voice_path is None:
            raise RuntimeError(f"the voice {self._voice_name!r} is not installed")
        return await asyncio.create_subprocess_exec(
            self._binary, *self._prefix_args,
            "--model", str(self._voice_path), "--output-raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=piper_environment(),
        )

    async def _stop(self, process: asyncio.subprocess.Process | None) -> None:
        """End a process that is not (or no longer) in service."""
        if process is None:
            return
        # Close the write transport before killing.  On Windows a killed child
        # with stdin still open can outlive `wait()` at transport teardown and
        # produce a misleading "Close running child process" warning.
        stdin = process.stdin
        if stdin is not None:
            try:
                stdin.close()
            except (AttributeError, OSError):
                pass
        try:
            kill = getattr(process, "kill", None)
            if kill is not None:
                kill()
            else:  # pragma: no cover - a stand-in without kill()
                process.terminate()
        except (ProcessLookupError, OSError):
            pass
        wait = getattr(process, "wait", None)
        if wait is not None:
            try:
                await wait()
            except Exception:
                pass
        # Proactor pipe shutdown is reported to the loop a little after the
        # child exit notification.  Let it settle before an immediately
        # following event-loop teardown; this only runs on retirement/failure,
        # never on a spoken reply.
        await asyncio.sleep(0.05)

    async def _ensure_process(self) -> bool:
        """Start Piper if it is not already running. Idempotent, never restarts."""
        if self._process is not None and self._process.returncode is None:
            return True
        self._process = await self._spawn()
        # The pump outlives the turn: it is the single reader for this process,
        # and only this process. It exits when Piper closes stdout, so it must
        # not be keyed to a turn or it would steal audio from the one after it.
        self._task = asyncio.create_task(self._pump())
        return True

    async def send_text(self, turn_id: UUID, text: str) -> None:
        text = piper_input_text(text)
        if not text or not text.strip():
            return
        # A new turn while the previous one was still speaking means that reply
        # is stale. Stop it, but keep the process: the whole point is not to pay
        # the voice load again.
        if self._turn_id is not None and self._turn_id != turn_id:
            self._publish(self._turn_id)
        if self._turn_id != turn_id:
            self._turn_saw_audio = False
            self._turn_last_audio_at = 0.0
        self._turn_id = turn_id
        # If the background boot warm-up is still loading, wait for its process
        # rather than spawning another cold model beside it.  The lock is held
        # only through process selection and the tiny stdin write, never while
        # synthesizing or playing audio.
        async with self._process_lock:
            await self._ensure_process()
            assert self._process is not None and self._process.stdin is not None
            self._process.stdin.write((text.strip() + "\n").encode("utf-8"))
            await self._process.stdin.drain()

    async def _pump(self) -> None:
        """Read Piper's raw PCM and hand it on in chunks.

        One reader for the life of the process. Audio is attributed to whatever
        turn is current when it arrives, so a reply that streams faster than the
        speaker consumes it is never dropped — the same rule the cloud
        synthesizer follows. Stale turns are rejected by `turn_id` in the
        coordinator, not here.
        """
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                block = await process.stdout.read(READ_CHUNK_BYTES)
                if not block:
                    break
                turn_id = self._turn_id
                if turn_id is not None:
                    self._turn_saw_audio = True
                    if self._loop is not None:
                        self._turn_last_audio_at = self._loop.time()
                    self._publish(AudioChunk(turn_id, block))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            # Always end the turn, even if Piper died, so playback cannot hang.
            if self._turn_id is not None:
                self._publish(self._turn_id)

    def _publish(self, item: AudioChunk | UUID) -> None:
        if self._loop is None or self._loop.is_closed():
            return

        def put() -> None:
            self._audio.put_nowait(item)

        try:
            self._loop.call_soon_threadsafe(put)
        except RuntimeError:
            pass

    async def audio(self, turn_id: UUID | None = None):
        turn_id = turn_id or self._turn_id
        while True:
            item = await self._audio.get()
            if isinstance(item, UUID):
                if item == turn_id:
                    return
            elif item.turn_id == turn_id:
                yield item

    async def flush(self, turn_id: UUID) -> None:
        """Wait for the audio already asked for, then end the turn.

        Piper synthesizes each sentence as soon as its line arrives, so the audio
        is produced or on its way by the time the caller flushes. The end of a
        turn is the first instant none of it is coming, and that is detected by
        watching the reader: audio only reaches the caller through `_pump`, so a
        buffer that has stopped growing means the reply is complete. A quiet
        period is required rather than a single empty look, because the buffer is
        briefly empty between Piper's write chunks and returning then would cut
        the end off a sentence.

        Ending the turn is this method's job, and it must happen on every path.
        The old implementation ended it by closing stdin, which the pump noticed
        as EOF; a reused process cannot be closed, so the sentinel is published
        here instead. Forgetting it on the success path hangs playback forever,
        which is exactly what the first version of this rewrite did.

        The timeout only covers a wedged process. It ends the turn rather than
        hanging playback forever.
        """
        if self._turn_id != turn_id or self._task is None:
            return
        process = self._process
        if process is None or process.stdout is None:
            self._publish(turn_id)
            self._turn_id = None
            return
        try:
            await asyncio.wait_for(
                self._wait_until_quiet(process), timeout=self._turn_timeout)
        except (asyncio.TimeoutError, Exception):
            # Nothing more is coming but the stream never settled. End the turn
            # anyway: playback must never hang on a wedged process.
            pass
        finally:
            self._publish(turn_id)
            # The turn is over: clear it so the pump attributes nothing else
            # to it and a later cleanup cannot mistake it for a live turn.
            self._turn_id = None
            self._retire_when_idle()

    async def _wait_until_quiet(self, process: asyncio.subprocess.Process) -> None:
        """Return once audio has arrived and stopped arriving.

        Waiting for a quiet buffer alone is wrong, and the first version of this
        got it badly wrong: Piper takes most of two seconds to load the voice, so
        the buffer is empty for the whole start-up and a quiet spell is satisfied
        before a single byte exists. The turn was then declared over and the reply
        was silence. Audio must have been seen before its absence means anything.
        """
        quiet = 0.0
        while True:
            if process.returncode is not None or process.stdout is None:
                return
            # Let the pump hand over whatever has arrived before looking again.
            await asyncio.sleep(0.01)
            if self._turn_saw_audio and self._loop is not None:
                since_audio = self._loop.time() - self._turn_last_audio_at
                if since_audio >= QUIET_SETTLE_SECONDS:
                    return
                # A fresh block has arrived, so the subsequent quiet window
                # begins from that block rather than from the old poll.
                quiet = 0.0
                continue
            quiet += 0.01

    def _retire_when_idle(self) -> None:
        """Close the process after a quiet spell instead of holding it forever.

        Holding a process idle costs memory for no benefit once the conversation
        has moved on, and the cache and test runs each build a throwaway
        synthesizer. Retiring is safe between turns and must never happen during
        one, which is why this is scheduled from `flush` and cancelled by
        `send_text`.
        """
        if self._idle_seconds <= 0 or self._loop is None or self._loop.is_closed():
            return
        # Cancel any retirement already queued: this turn's activity means the
        # process is still earning its keep.
        for task in asyncio.all_tasks(self._loop):
            if task.get_name() == f"piper-idle-{id(self)}":
                task.cancel()
        try:
            self._loop.create_task(self._idle_retire(), name=f"piper-idle-{id(self)}")
        except RuntimeError:
            pass

    async def _idle_retire(self) -> None:
        try:
            await asyncio.sleep(self._idle_seconds)
        except asyncio.CancelledError:
            return
        # Retiring is the one cancel that really ends the process; a soft
        # cancel would leave it running forever past its idle window.
        await self.cancel(self._turn_id, retire=True)

    async def cancel(self, turn_id: UUID | None, *, retire: bool = False) -> None:
        """End a turn's audio immediately — without killing the process.

        Ending a turn is not the same as retiring the process. A barge-in, or
        the coordinator's cleanup after a reply has fully played, only needs
        this turn to stop: the sentinel is published so any consumer returns,
        and the pump drops whatever Piper is still producing. The process
        itself stays warm, because killing it here made every reply pay the
        ~1.8s voice load again on the next turn — the one cost this backend
        exists to avoid. `retire=True` (idle expiry, close) is the only path
        that really throws the process away.
        """
        if not retire and self._turn_id is not None and self._turn_id != turn_id:
            # A newer turn owns the synthesizer already. End only the stale
            # one; its sentinel still goes out so nothing waits on it.
            if turn_id is not None:
                self._publish(turn_id)
            return
        if retire:
            process, self._process = self._process, None
            task, self._task = self._task, None
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if process is not None:
                try:
                    kill = getattr(process, "kill", None)
                    if kill is not None:
                        kill()
                    else:  # pragma: no cover - a stand-in without kill()
                        process.terminate()
                except ProcessLookupError:
                    pass
                wait = getattr(process, "wait", None)
                if wait is not None:
                    await wait()
            if turn_id is not None:
                self._publish(turn_id)
            return
        # End of the current turn only. Clearing _turn_id makes the pump drop
        # the rest of this reply while keeping the process ready for the next.
        self._turn_id = None
        if turn_id is not None:
            self._publish(turn_id)

    async def close(self) -> None:
        await self.cancel(self._turn_id, retire=True)
