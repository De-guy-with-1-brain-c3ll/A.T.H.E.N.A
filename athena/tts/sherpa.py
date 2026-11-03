"""Local speech through sherpa-onnx: no network, no API key, no per-character cost.

One synthesizer for several model families, because the right family depends on
the machine and that is not knowable in advance. It runs *in this process*, which
is the whole point: Piper needed a subprocess, a line-fed stdin, a stdout pump and
a turn-end sentinel published on every path, and that protocol is where its
hardest bug lived. sherpa-onnx removes all of it — the same library the local
recogniser already uses.

The family is detected from the model directory, and the choice matters more than
any other setting here. Measured on an Orange Pi Zero 3 (4 cores, NEON, **no
int8 dot-product**), 2 threads:

    family          model                     load    realtime
    vits            lessac-medium, 63 MB      7.6 s   0.89x   <- works here
    kitten          nano int8, 24 MB          5.5 s   0.30x   <- 3x too slow
    (cloud)         qwen3-tts-flash-realtime  0.6 s   realtime

**The desktop does not predict the board, and the gap is not small.** On a desktop
Kitten measured 2.5-2.7x and VITS 26x. On the Pi Kitten collapsed to 0.30x while
VITS held 0.89x — a 9x fall against 30x. The cause is the missing int8
acceleration: quantised kernels fall back to generic paths, so an int8 model is
penalised far more than its size suggests, and VITS's fp32 graph is not. A 12.8 s
reply takes 41 s to synthesize under Kitten, which drains the speaker dry; the
same reply costs 14.5 s under VITS, which keeps up.

Two threads is the default and four is *worse* (0.66x against 0.89x, and 0.26x
against 0.30x for Kitten) — this board is memory-bandwidth-bound, so extra threads
buy nothing and take cores from capture and playback.

Neither local model is streaming: each sees a finished sentence and returns the
whole waveform. The bridge to ATHENA's streaming playback is to synthesize on a
worker thread and publish the PCM in chunks, so the speaker starts playing while
the rest is still being handed over.

Voice quality is clearly not the cloud voice, and `ATHENA_TTS_BACKEND=qwen` keeps
that for when it matters. What the local voice is, is intelligible: every clip
benchmarked was transcribed back through the local recogniser and every fact
survived, including the CJ name and the 430 time.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from uuid import UUID

from athena.events import AudioChunk


# 24 kHz for Kitten and the cloud voice; VITS speaks at 22.05 kHz. The value here
# is only a fallback — the model reports its own rate at load and `connect`
# adopts it, because opening the speaker at the wrong rate plays the reply at the
# wrong speed, which is far harder to diagnose than an error would be.
DEFAULT_SAMPLE_RATE = 24_000

# The VITS Piper voice, because it is the one that actually keeps up on the
# board. It is the same lessac-medium voice the Piper install already uses, just
# in sherpa's layout — so switching to it costs nothing in voice quality and
# removes the subprocess.
DEFAULT_MODEL = "vits-piper-en_US-lessac-medium"

# How much PCM is handed to the speaker at a time. Large enough that a queue put
# is not per-sample, small enough that playback starts promptly and a cancelled
# turn does not have to throw away a whole sentence of buffered audio.
CHUNK_BYTES = 8_192
# 8192 bytes is 273 ms of 24 kHz mono 16-bit. Halved again for the sentinel
# cadence: the pump publishes the end of a turn as soon as the last chunk is in,
# so this only bounds how long `.audio()` can sit on audio it already has.
PUBLISH_INTERVAL_SECONDS = 0.05

# The model is 24 MB and loads in under a second here, but a Pi is far slower and
# the load blocks, so it happens on a worker thread; this bounds the wait.
MODEL_LOAD_TIMEOUT_SECONDS = 180.0
# A single sentence on a slow board. Bounded so a wedged worker costs one reply
# rather than the whole service.
SYNTHESIS_TIMEOUT_SECONDS = 120.0

# Kitten reads phonemes from text through espeak, so there is a ceiling on how
# much it will accept at once. Long text is split at sentence boundaries and
# synthesized piece by piece, which is also what keeps the first audio early.
MAX_SYNTHESIS_CHARS = 400


def models_directory() -> Path:
    """Where the Kitten files live.

    The default is the same place the benchmark used, so a machine that has run
    `tools/install_sherpa_tts.sh` needs no configuration.
    """
    configured = os.environ.get("ATHENA_SHERPA_MODEL_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path("outputs") / "models" / "kitten"


def sherpa_model_name() -> str:
    return os.environ.get("ATHENA_SHERPA_MODEL", "").strip() or DEFAULT_MODEL


def sherpa_model_directory() -> Path:
    """The directory holding one named model, tolerating a bare path."""
    configured = os.environ.get("ATHENA_SHERPA_MODEL_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser()
        # A user who points straight at a model directory means that directory.
        if (root / "tokens.txt").is_file():
            return root
        return root / sherpa_model_name()
    return models_directory() / sherpa_model_name()


def sherpa_precision() -> str:
    return os.environ.get("ATHENA_SHERPA_PRECISION", "int8").strip().casefold()


def sherpa_family(directory: Path | None = None) -> str:
    """Which model family this directory holds, read from its own name.

    The families need different config objects and sherpa reports the mismatch
    as a complaint about ONNX metadata, which sends you looking at the model file
    rather than at the config. Detecting it from the directory name — the same
    convention the download scripts and the benchmark use — keeps that failure
    out of reach. An explicit override wins, for a directory named anything else.
    """
    configured = os.environ.get("ATHENA_SHERPA_FAMILY", "").strip().casefold()
    if configured:
        return configured
    name = (directory or sherpa_model_directory()).name.casefold()
    if "vits" in name or "piper" in name:
        return "vits"
    if "kokoro" in name:
        return "kokoro"
    if "matcha" in name:
        return "matcha"
    return "kitten"


def sherpa_model_file(directory: Path | None = None) -> Path:
    """The ONNX file to load.

    Found rather than constructed, because the families disagree about the name:
    Kitten ships `model.int8.onnx` and `model.onnx`, while a VITS voice is named
    after the voice itself (`en_US-lessac-medium.onnx`). Guessing the name is how
    a working model gets reported as "not there".
    """
    folder = directory or sherpa_model_directory()
    if sherpa_family(folder) == "kitten":
        # Kitten does ship both, so the precision setting is meaningful here.
        preferred = ("model.int8.onnx", "model.onnx") if sherpa_precision() in {
            "int8", "quantized", "quantised"} else ("model.onnx", "model.int8.onnx")
        for name in preferred:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    matches = sorted(folder.glob("*.onnx"))
    if matches:
        return matches[0]
    # Nothing found: return the path we would have wanted, so the "not there"
    # message names a real location rather than an empty string.
    return folder / ("model.int8.onnx" if sherpa_family(folder) == "kitten"
                     else "model.onnx")


def sherpa_tokens_file(directory: Path | None = None) -> Path:
    return (directory or sherpa_model_directory()) / "tokens.txt"


def sherpa_voices_file(directory: Path | None = None) -> Path:
    """The speaker table, which only the multi-speaker families have.

    Kitten has eight voices in one `voices.bin`; a VITS voice like lessac-medium
    is a single speaker with no table at all. Requiring one would refuse a voice
    that works perfectly well.
    """
    return (directory or sherpa_model_directory()) / "voices.bin"


def sherpa_needs_voices(directory: Path | None = None) -> bool:
    """Whether this family needs the voices table to load at all."""
    return sherpa_family(directory) in {"kitten", "kokoro"}


def sherpa_data_directory(directory: Path | None = None) -> Path:
    """The bundled espeak data that turns text into phonemes.

    Kitten ships its own, which is an advantage over Piper: Piper needs
    espeak-ng installed system-wide, so the voice is useless until apt has run.
    """
    return (directory or sherpa_model_directory()) / "espeak-ng-data"


def sherpa_speaker() -> int:
    """Which of the eight voices to speak with."""
    try:
        return max(0, int(os.environ.get("ATHENA_SHERPA_SPEAKER", "0")))
    except ValueError:
        return 0


def sherpa_speed() -> float:
    """Speaking rate, 1.0 being the model's own pace."""
    try:
        return max(0.25, min(4.0, float(os.environ.get("ATHENA_SHERPA_SPEED", "1.0"))))
    except ValueError:
        return 1.0


def sherpa_sample_rate() -> int:
    """The rate the speaker must be opened at.

    The speaker is opened once, at start-up, from this value — before the model
    is loaded and can report its own rate. So this has to be right in advance,
    and getting it wrong is not an error: it is the reply played at the wrong
    speed, which is much harder to attribute.

    The default follows the family rather than being one number, because the
    families genuinely differ: a VITS Piper voice is 22.05 kHz and Kitten is
    24 kHz. Setting it by hand is only needed for an unusual model.
    """
    default = 22_050 if sherpa_family() == "vits" else DEFAULT_SAMPLE_RATE
    try:
        return int(os.environ.get("ATHENA_SHERPA_SAMPLE_RATE", str(default)))
    except ValueError:
        return default


def sherpa_threads() -> int:
    """CPU threads for synthesis.

    Two by default, and the measurement is why: four threads were 2.79x against
    two threads' 2.70x, which is not worth the two cores the capture loop and
    the speaker want on a four-core board.
    """
    raw = os.environ.get("ATHENA_SHERPA_THREADS", "").strip() or "2"
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def sherpa_idle_seconds() -> float:
    """How long an unused model is kept loaded. Zero keeps it forever, and is
    the default.

    Retiring the model saves its memory — 63 MB for the VITS voice plus its
    espeak data — and costs the **7.6 s reload** measured on an Orange Pi Zero 3.
    That is the wrong trade for a voice assistant: the first thing said after a
    pause is the reply a person is most likely to be standing there waiting for,
    and 75 MB is nothing on a board with 2.7 GB free. Retiring is also what made
    `send_text` publish a silent turn, so the safer default is not to retire.

    Set a positive value to retire anyway; the reload now works, so the cost is
    the delay rather than silence.
    """
    try:
        return max(0.0, float(os.environ.get("ATHENA_SHERPA_IDLE_SECONDS", "0")))
    except ValueError:
        return 300.0


def sherpa_available() -> tuple[bool, str]:
    """Whether local speech can run here, and why not when it cannot.

    The import and the files are reported separately because they have different
    fixes: one is `pip install sherpa-onnx`, the other is running the installer.
    The voices table is only required by the families that have one, so a
    single-speaker VITS voice is not rejected for lacking it.
    """
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False, "sherpa-onnx is not installed (pip install sherpa-onnx)"
    directory = sherpa_model_directory()
    if not sherpa_model_file(directory).is_file():
        return False, f"the model {sherpa_model_file(directory)} is not there"
    if not sherpa_tokens_file(directory).is_file():
        return False, f"the tokens file {sherpa_tokens_file(directory)} is not there"
    if sherpa_needs_voices(directory) and not sherpa_voices_file(directory).is_file():
        return False, f"the voices file {sherpa_voices_file(directory)} is not there"
    return True, ""


def split_for_synthesis(text: str, limit: int = MAX_SYNTHESIS_CHARS) -> list[str]:
    """Break a reply into pieces small enough for one synthesis call.

    Splitting happens at sentence ends where possible, because the joins are
    audible: a piece boundary mid-clause reads as an unnatural pause. A single
    clause longer than the limit is cut at a space anyway rather than refused —
    a long reply should sound slightly odd, never go missing.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    if len(cleaned) <= limit:
        return [cleaned]
    pieces: list[str] = []
    remaining = cleaned
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "),
                  window.rfind("; "), window.rfind(", "))
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit - 1
        pieces.append(remaining[:cut + 1].strip())
        remaining = remaining[cut + 1:].strip()
    if remaining:
        pieces.append(remaining)
    return [piece for piece in pieces if piece]


class SherpaSynthesizer:
    """Local KittenTTS behind the same interface as the cloud synthesizer.

    The coordinator drives synthesis identically for every backend, so what this
    has to reproduce is the *shape* of the conversation: sentences in, audio
    chunks out under a turn id, and a bare `UUID` in the queue when the turn is
    finished so playback knows to stop.

    Unlike the Piper synthesizer there is no subprocess and no pump reading its
    stdout. Synthesis is one blocking call per sentence on a worker thread, and
    the result is published to the queue directly. That removes the single reader
    and the delicate part of the turn protocol — where Piper's pump had to notice
    a closing stdin, and its `flush` had to publish the sentinel on every path or
    the next `audio()` waited forever. Here the sentinel is the only thing that
    ends a turn, and `flush` and `cancel` both publish it.

    The idle retirement is kept: the model is dropped after
    `ATHENA_SHERPA_IDLE_SECONDS` of quiet rather than held for the process's life.
    """

    sample_rate = DEFAULT_SAMPLE_RATE

    def __init__(
        self,
        model_dir: Path | str | None = None,
        speaker: int | None = None,
        speed: float | None = None,
        threads: int | None = None,
        sample_rate: int | None = None,
        settings=None,
        idle_seconds: float | None = None,
    ) -> None:
        self._directory = Path(model_dir) if model_dir else sherpa_model_directory()
        self._speaker = sherpa_speaker() if speaker is None else max(0, int(speaker))
        self._speed = sherpa_speed() if speed is None else float(speed)
        self._threads = sherpa_threads() if threads is None else max(1, int(threads))
        self.sample_rate = sample_rate or sherpa_sample_rate()
        # What the model itself reports once loaded, for comparison with the rate
        # the speaker was opened at. A disagreement is audible as wrong-speed
        # speech and otherwise leaves no trace, so it is kept for the log.
        self.reported_sample_rate = 0
        self._settings = settings
        self._idle_seconds = sherpa_idle_seconds() if idle_seconds is None else idle_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._audio: asyncio.Queue[AudioChunk | UUID] = asyncio.Queue()
        self._tts = None
        self._turn_id: UUID | None = None
        self._idle_task: asyncio.Task | None = None
        # The warm-up, when one is running. `send_text` waits on it rather than
        # racing it, because the coordinator starts it without awaiting.
        self._warm_task: asyncio.Task | None = None
        # Serialises synthesis. Two turns' audio interleaved in one queue would
        # be spoken as one garbled reply, and the model is not reentrant.
        self._lock = asyncio.Lock()
        # Latency accounting, so the effect of a change is measurable on the Pi
        # and comparable with the Piper and cloud backends.
        self.last_load_ms = 0.0
        self.last_synthesis_ms = 0.0
        self.sentences = 0

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Load the model, off the event loop.

        This is the expensive step and it happens once. It is raised on failure
        rather than logged, because a synthesizer that cannot load anything
        would otherwise look like a speaker that is simply silent.
        """
        self._loop = asyncio.get_running_loop()
        available, reason = self._availability()
        if not available:
            raise RuntimeError(f"Local speech is not usable: {reason}")
        started = time.monotonic()
        try:
            async with asyncio.timeout(MODEL_LOAD_TIMEOUT_SECONDS):
                await asyncio.to_thread(self._load)
        except TimeoutError:
            raise RuntimeError(
                "Loading the local speech model took too long. "
                f"Check that {sherpa_model_file(self._directory)} is a complete "
                "download.") from None
        self.last_load_ms = (time.monotonic() - started) * 1000

    def _availability(self) -> tuple[bool, str]:
        """Check this instance, not the environment.

        The tests pass explicit paths, and a user may too, so the configured
        defaults are only the fallback — the same rule the recogniser follows.
        """
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False, "sherpa-onnx is not installed (pip install sherpa-onnx)"
        model = sherpa_model_file(self._directory)
        if not model.is_file() or model.stat().st_size == 0:
            return False, f"the model {model} is not there"
        if not sherpa_tokens_file(self._directory).is_file():
            return False, f"the tokens file {sherpa_tokens_file(self._directory)} is not there"
        if (sherpa_needs_voices(self._directory)
                and not sherpa_voices_file(self._directory).is_file()):
            return False, f"the voices file {sherpa_voices_file(self._directory)} is not there"
        return True, ""

    def _load(self) -> None:
        """Build the config for this family and load the model.

        Each family needs its own config object, and passing the wrong one fails
        with a complaint about ONNX metadata rather than about the family — so
        the dispatch is explicit and the family is detectable in advance.
        """
        import sherpa_onnx

        family = sherpa_family(self._directory)
        common = {
            "model": str(sherpa_model_file(self._directory)),
            "tokens": str(sherpa_tokens_file(self._directory)),
            "data_dir": str(sherpa_data_directory(self._directory)),
        }
        if family == "vits":
            # VITS takes text and phonemises internally; it has no voices table.
            family_config = sherpa_onnx.OfflineTtsVitsModelConfig(**common)
        elif family == "kokoro":
            family_config = sherpa_onnx.OfflineTtsKokoroModelConfig(**common)
        elif family == "matcha":
            family_config = sherpa_onnx.OfflineTtsMatchaModelConfig(**common)
        else:
            family_config = sherpa_onnx.OfflineTtsKittenModelConfig(
                voices=str(sherpa_voices_file(self._directory)), **common)

        model = sherpa_onnx.OfflineTtsModelConfig(
            num_threads=self._threads, debug=False, **{family: family_config})
        self._tts = sherpa_onnx.OfflineTts(
            sherpa_onnx.OfflineTtsConfig(model=model))
        # Adopt the model's own rate rather than trusting the configured one. The
        # speaker is opened before this runs, so a disagreement here means the
        # reply will play at the wrong speed — worth recording for the log even
        # though it cannot be corrected from here.
        reported = int(getattr(self._tts, "sample_rate", 0) or 0)
        if reported:
            self.reported_sample_rate = reported

    async def warm(self) -> bool:
        """Load the model now so the first reply does not wait for it.

        Best-effort by design: a failure here costs the next reply its head
        start, which is what would have happened anyway, so it must not be able
        to break a turn that is still perfectly able to speak. The task is kept
        so `send_text` can wait for an in-flight load instead of racing it.
        """
        if self._tts is not None:
            return True
        self._warm_task = asyncio.current_task()
        try:
            await self.connect()
            return True
        except Exception:
            return False
        finally:
            self._warm_task = None

    async def cache_phrase(self, text: str) -> bytes:
        """Synthesize without playing, used to pre-render the fixed phrases.

        Returns raw PCM rather than a WAV container, matching the Piper and
        cloud synthesizers so the coordinator can treat all three the same.
        """
        chunks = [chunk async for chunk in self._synthesize(text)]
        return b"".join(chunks)

    # -- one turn ----------------------------------------------------------

    async def send_text(self, turn_id: UUID, text: str) -> None:
        """Synthesize a reply and publish it under this turn.

        The work is done here rather than in `flush` because the coordinator
        calls `send_text` as sentences become available, and synthesizing a
        sentence while the previous one is still being spoken is the whole point
        of a local voice: the speaker never runs dry.
        """
        if not text or not text.strip():
            return
        # A new turn while the previous one was still speaking means that reply
        # is stale. End it, but keep the model — reloading is the expensive part.
        if self._turn_id is not None and self._turn_id != turn_id:
            self._publish(self._turn_id)
        self._turn_id = turn_id
        self._cancel_idle()
        # The model may be absent for two very different reasons: a warm-up that
        # has not finished, or one that was retired after a quiet spell. Both
        # need the model loaded before this reply can be spoken, and the second
        # is the ordinary case for the first thing said after a pause.
        if not await self._ensure_ready():
            # A synthesizer that could not load cannot speak. Ending the turn
            # here is what keeps playback from waiting forever for audio that is
            # never coming.
            self._publish(turn_id)
            return
        async with self._lock:
            try:
                async for chunk in self._synthesize(text):
                    # The turn may have been superseded while this ran.
                    if self._turn_id != turn_id:
                        return
                    self._publish(AudioChunk(turn_id, chunk))
            except Exception:
                # Never leave the turn open: playback must not hang on a
                # synthesis failure, it must simply be silent.
                pass

    async def _ensure_ready(self) -> bool:
        """Make sure the model is loaded, loading it if it was retired.

        This used to only *wait* for a warm-up that was already in flight, and
        that was a bug with a nasty shape. `_idle_retire` drops the model after
        `ATHENA_SHERPA_IDLE_SECONDS`, and `warm` clears `_warm_task` when it
        finishes — so after a quiet spell there was no model *and* nothing to
        wait on. The wait returned immediately, `send_text` saw no model,
        published the turn sentinel with no audio, and **nothing ever started a
        new load**, so every reply after the first pause was silent for the life
        of the process. It looked like the voice had broken rather than expired.

        Starting the load here is the whole fix. It costs the reload time on the
        first reply after a pause, which is the honest price of retiring at all;
        `ATHENA_SHERPA_IDLE_SECONDS=0` avoids even that by never retiring.
        """
        if self._tts is not None:
            return True
        task = self._warm_task
        # Only wait on a load someone else started, and never on ourselves —
        # `warm` would otherwise record this turn as the warm-up task.
        if task is not None and not task.done() and task is not asyncio.current_task():
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            if self._tts is not None:
                return True
        try:
            await self.connect()
            return True
        except Exception:
            return False

    async def _synthesize(self, text: str):
        """Yield PCM for one reply, sentence by sentence.

        Each piece is synthesized on a worker thread: the call releases the GIL
        while it runs, but it takes hundreds of milliseconds and it must not be
        awaited on the event loop. Pieces are yielded in order so the audio
        comes out as it is produced rather than at the end of the reply.
        """
        for piece in split_for_synthesis(text):
            started = time.monotonic()
            samples = await asyncio.to_thread(self._generate, piece)
            self.last_synthesis_ms = (time.monotonic() - started) * 1000
            self.sentences += 1
            if samples is None:
                continue
            pcm = _pcm_bytes(samples)
            for start in range(0, len(pcm), CHUNK_BYTES):
                yield pcm[start:start + CHUNK_BYTES]
                # Hand control back so the speaker can start playing and a
                # superseded turn is noticed before the rest is synthesized.
                await asyncio.sleep(0)

    def _generate(self, text: str):
        """Run the model. Blocking, and called on a worker thread.

        Returns the audio as raw PCM bytes, which is the format every other
        synthesizer in this project produces and the format the speaker reads.

        sherpa-onnx returns the waveform as a plain list of normalised floats,
        which is the opposite of what the speaker wants: handing those to
        `tobytes` would produce 8-byte doubles that the device reads as noise at
        a quarter speed. They have to be quantised to the 16-bit samples every
        other backend produces, and clipped on the way, because a sample past
        full scale cast straight to int overflows into a click.
        """
        import array

        if self._tts is None:
            return None
        audio = self._tts.generate(text, sid=self._speaker, speed=self._speed)
        samples = getattr(audio, "samples", None)
        if samples is None:
            return b""
        values = array.array("h", (_clip(sample) for sample in samples))
        return _pcm_bytes(values)

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

        `send_text` has already produced the audio by the time this is called,
        so there is nothing to wait for but the queue to drain. The sentinel is
        published here on every path — including the one where a superseded turn
        changed `_turn_id` underneath — because forgetting it hangs playback
        forever, which is exactly the bug the Piper rewrite hit.

        The wait bounds a wedged worker rather than a normal reply.
        """
        if self._turn_id != turn_id:
            return
        try:
            async with asyncio.timeout(SYNTHESIS_TIMEOUT_SECONDS):
                # Give the speaker a moment to consume what is queued. The
                # sentinel only marks the end of *production*; playback reads
                # the queue in order, so the audio ahead of it is not lost.
                while self._lock.locked():
                    await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)
        except TimeoutError:
            pass
        finally:
            self._publish(turn_id)
            self._retire_when_idle()

    # -- queue and lifetime -------------------------------------------------

    def _publish(self, item: AudioChunk | UUID) -> None:
        if self._loop is None or self._loop.is_closed():
            return

        def put() -> None:
            self._audio.put_nowait(item)

        try:
            self._loop.call_soon_threadsafe(put)
        except RuntimeError:
            pass

    async def cancel(self, turn_id: UUID | None) -> None:
        """Stop speaking and end the turn. The model stays loaded.

        Audio already queued for this turn is discarded, not just superseded.
        An interruption means the reply is no longer wanted, and the queue may
        hold several hundred milliseconds of it that `send_text` published
        before the interruption arrived. The coordinator cancels playback first,
        so leaving them would normally be harmless — but "normally" depends on
        the caller getting the order right, and a stale reply that plays after
        an interruption is a bug that is very hard to attribute.
        """
        if turn_id is not None and self._turn_id == turn_id:
            self._turn_id = None
        if turn_id is not None:
            self._discard(turn_id)
            self._publish(turn_id)

    def _discard(self, turn_id: UUID) -> None:
        """Remove one turn's queued audio, keeping everything else in order.

        The queue is drained and rebuilt rather than filtered in place because
        `asyncio.Queue` has no removal, and the order of what remains matters:
        the sentinel for a still-open turn must stay behind its own audio.
        """
        kept: list[AudioChunk | UUID] = []
        while not self._audio.empty():
            try:
                item = self._audio.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - the loop just checked
                break
            if isinstance(item, AudioChunk) and item.turn_id == turn_id:
                continue
            kept.append(item)
        for item in kept:
            self._audio.put_nowait(item)

    async def close(self) -> None:
        self._cancel_idle()
        self._tts = None
        if self._turn_id is not None:
            self._discard(self._turn_id)
            self._publish(self._turn_id)
            self._turn_id = None

    def _cancel_idle(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _retire_when_idle(self) -> None:
        """Drop the model after a quiet spell instead of holding it forever.

        Holding it costs 24 MB and, more to the point, keeps a loaded ONNX
        session alive for a conversation that has stopped. Retiring is safe
        between turns and must never happen during one, which is why it is
        scheduled from `flush` and cancelled by `send_text`.
        """
        if self._idle_seconds <= 0 or self._loop is None or self._loop.is_closed():
            return
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        try:
            self._idle_task = self._loop.create_task(self._idle_retire())
        except RuntimeError:
            pass

    async def _idle_retire(self) -> None:
        try:
            await asyncio.sleep(self._idle_seconds)
        except asyncio.CancelledError:
            return
        self._tts = None


def _clip(sample: float) -> int:
    """A normalised float sample as a 16-bit integer, without overflowing.

    Scaling by 32767 rather than 32768 is deliberate: `array("h", [32768])`
    raises OverflowError instead of wrapping, so a sample at or over full scale
    would take the whole reply down rather than play loudly. The clamp uses the
    same bound so the mapping stays monotonic — clamping to -32768 while scaling
    from -32767 would make the two ends of the range behave differently for no
    reason anyone could explain later.
    """
    value = int(round(float(sample) * 32767.0))
    return max(-32767, min(32767, value))


def _pcm_bytes(values) -> bytes:
    """16-bit little-endian PCM bytes from a sample array.

    A straight byte view is native-endian, which would be byte-swapped on a
    big-endian machine. Every relevant board is little-endian, so the copy is
    normally wasted — but it is one allocation against a frame of noise that
    would be very hard to attribute to this line.
    """
    import array
    import sys

    if not isinstance(values, array.array) or values.typecode != "h":
        values = array.array("h", values)
    else:
        # Work on a copy: byteswap mutates in place, and on a big-endian machine
        # this would otherwise alter the caller's buffer as a side effect.
        values = array.array("h", values)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()
