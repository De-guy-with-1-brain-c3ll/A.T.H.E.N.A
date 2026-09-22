"""Local speech recognition with SenseVoice-Small, through sherpa-onnx.

The cloud recognisers bill by the second: ¥0.00033 per audio second, about
¥1.19 an hour of speech, on every model the platform offers. Nothing about that
price can be tuned by picking a different one. The only way down is to stop
sending the audio anywhere, which is what this backend does — it runs entirely
on the board, costs nothing per hour, needs no API key and works with no
network at all.

Measured 2026-09-17 on this project's own clips: 45-75x realtime on four
threads, 22x on a single thread. A three second utterance decodes in 0.049s, so
decoding is not the cost of a turn. The cost is the 229 MB model load, and that
is paid once at start-up rather than per turn — the same reason the Piper
synthesizer holds its process open.

The central problem this module solves is that SenseVoice is not a streaming
model. It sees a finished utterance and returns text, while the coordinator
wants the cloud shape: audio arrives in 100 ms packets and partial transcripts
trickle back as someone speaks. The bridge is to keep a growing audio buffer and
re-decode it — on a background worker, never on the event loop — each time
enough new speech has arrived. Because decoding is ~50x realtime, re-reading the
whole utterance every time is affordable, and it is what makes the partials
*replace* each other the way the cloud partials do, instead of accumulating.
"""
from __future__ import annotations

import asyncio
from collections import deque
import os
from pathlib import Path
import time
from uuid import UUID

from athena.events import Transcript
from athena.stt.fun_asr import PACKET_BYTES


# SenseVoice reads 16 kHz mono. The microphone already delivers that, and the
# model resamples anyway, so this is only the format audio must arrive in.
DEFAULT_SAMPLE_RATE = 16_000

# How much new audio must arrive before a partial is worth producing. SenseVoice
# needs a little context to be meaningful — one 100 ms packet contains no words —
# so partials are emitted at most after this much new speech. 800 ms keeps the
# first partial early enough to feel responsive while giving the model a syllable
# or two of real content to work with.
PARTIAL_INTERVAL_SECONDS = 0.8
# The shortest utterance that is decoded at all. Below this there is nothing to
# recognise, and running the model on 200 ms of room tone produces a stray word.
MINIMUM_DECODE_SECONDS = 0.4
# Audio kept for the turn. Beyond this the oldest is dropped: an open microphone
# should not grow without limit, and no transcript needs ten minutes of context.
MAX_TURN_SECONDS = 120.0

# The model is ~230 MB of ONNX. Loading it blocks, so it happens on a worker
# thread; this bounds how long the first turn will wait for it.
MODEL_LOAD_TIMEOUT_SECONDS = 180.0
# A single decode of a long utterance, bounded so a wedged thread costs one
# partial instead of the turn.
DECODE_TIMEOUT_SECONDS = 30.0

# SenseVoice emits bracketed event and language tags ahead of the words, for
# example "<|en|><|NEUTRAL|><|Speech|><|woitn|>Hello there." Left in place they
# would be spoken aloud by the answer pipeline and counted as words in the
# transcript. The cloud models return none of this, so they are removed here.
TAG_OPEN = "<|"
TAG_CLOSE = "|>"
# SenseVoice tags everything, including the utterance's own language and the
# inverse-text-normalisation marker, so a leading run of them is expected.
MAX_TAGS = 8


def model_directory() -> Path:
    """Where the SenseVoice files live.

    The default is the same place the benchmark used, so a machine that has run
    `tools/download_sensevoice.sh` needs no configuration.
    """
    configured = os.environ.get("ATHENA_SENSEVOICE_MODEL_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path("outputs") / "models" / "sensevoice"


def sensevoice_model_file() -> Path:
    """The precision to load.

    int8 by default, and the numbers are why: it is 228 MB against 938 MB and
    about 2.5x faster, for a word error rate of 0.188 against 0.171. On a 4 GB
    board the memory and the speed are worth more than that difference, and a
    free recogniser that is 10% less accurate still beats one that bills by the
    second.
    """
    precision = os.environ.get("ATHENA_SENSEVOICE_PRECISION", "int8").strip().casefold()
    name = "model.onnx" if precision in {"fp32", "float32", "full"} else "model.int8.onnx"
    return model_directory() / name


def sensevoice_tokens_file() -> Path:
    return model_directory() / "tokens.txt"


def sensevoice_threads() -> int:
    """CPU threads for the decoder.

    Two is the default because the Pi Zero 3 has four cores and the voice
    service shares them with capture, playback and the language model client.
    Even one thread is 22x realtime, so there is a lot of headroom to give away.

    `sherpa_threads` is accepted as well because the benchmark scripts already
    use that name and it would be a trap for them to configure a different value
    than the service runs with.
    """
    raw = (os.environ.get("ATHENA_SENSEVOICE_THREADS", "").strip()
           or os.environ.get("sherpa_threads", "").strip() or "2")
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def sensevoice_available() -> tuple[bool, str]:
    """Whether local recognition can run here, and why not when it cannot.

    Both halves matter and both are reported separately: a missing import is a
    packaging problem, and a missing model is a download the user has to run.
    They have different fixes, so they get different messages.
    """
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False, ("sherpa-onnx is not installed "
                       "(pip install sherpa-onnx)")
    model = sensevoice_model_file()
    if not model.is_file() or model.stat().st_size == 0:
        return False, f"the model {model} is not there"
    if not sensevoice_tokens_file().is_file():
        return False, f"the tokens file {sensevoice_tokens_file()} is not there"
    return True, ""


def strip_tags(text: str) -> str:
    """Remove SenseVoice's bracketed markers, keeping the words.

    The tags arrive at the front and are run together — `<|en|><|NEUTRAL|>
    <|Speech|><|woitn|>` — so the leading run is removed and anything left
    behind elsewhere is removed too rather than left to be spoken. A stray `<`
    from a half-recognised tag would otherwise survive into the transcript.
    """
    remaining = text.lstrip()
    for _ in range(MAX_TAGS):
        if not remaining.startswith(TAG_OPEN):
            break
        end = remaining.find(TAG_CLOSE)
        if end < 0:
            break
        remaining = remaining[end + len(TAG_CLOSE):].lstrip()
    while TAG_OPEN in remaining:
        start = remaining.find(TAG_OPEN)
        end = remaining.find(TAG_CLOSE, start)
        if end < 0:
            remaining = remaining[:start]
            break
        remaining = remaining[:start] + remaining[end + len(TAG_CLOSE):]
    return remaining.strip()


class _TurnBuffer:
    """The audio of one turn, in the format the model wants.

    Kept as a deque of packet-sized chunks rather than one bytearray so that
    dropping the oldest audio is a popleft instead of a reallocation of the
    whole utterance, which on a turn that has been running for a minute would be
    megabytes copied per packet.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.chunks: deque[bytes] = deque()
        self.bytes = 0

    def append(self, pcm: bytes) -> None:
        self.chunks.append(pcm)
        self.bytes += len(pcm)
        limit = int(MAX_TURN_SECONDS * self.sample_rate) * 2
        while self.bytes > limit and len(self.chunks) > 1:
            self.bytes -= len(self.chunks.popleft())

    @property
    def seconds(self) -> float:
        return self.bytes / (self.sample_rate * 2)

    def samples(self) -> list[float]:
        """Normalised float samples, which is what accept_waveform wants.

        The int16 conversion is done in Python rather than with array/struct
        because it has to cross into the extension module as a sequence anyway;
        a generator over memoryview keeps the peak allocation to one list.
        """
        raw = b"".join(self.chunks)
        import array
        values = array.array("h")
        values.frombytes(raw)
        return [sample / 32768.0 for sample in values]

    def clear(self) -> None:
        self.chunks.clear()
        self.bytes = 0


class SenseVoiceRecognizer:
    """Local SenseVoice-Small behind the same interface as the cloud recogniser.

    The coordinator drives STT identically for every backend, so what this class
    has to reproduce is not just the method signatures but the *shape* of the
    conversation: audio in packets, partials that replace each other while
    someone speaks, one final transcript, and a `[STT complete]` sentinel when
    the turn ends without words.

    The model runs on a worker thread. `decode_stream` releases the GIL while it
    runs, so a decode does not stop audio capture, but it does take tens of
    milliseconds and it must not be awaited on the event loop.
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        model_file: Path | str | None = None,
        tokens_file: Path | str | None = None,
        threads: int | None = None,
        language: str = "auto",
        use_itn: bool = True,
    ) -> None:
        self._sample_rate = sample_rate
        self._model_file = Path(model_file) if model_file else sensevoice_model_file()
        self._tokens_file = Path(tokens_file) if tokens_file else sensevoice_tokens_file()
        self._threads = sensevoice_threads() if threads is None else threads
        # "auto" lets one model handle the English with Chinese words in it that
        # the cloud hint list exists to handle. SenseVoice also accepts "en",
        # "zh", "ja", "ko" and "yue"; forcing one hurts mixed speech.
        self._language = language
        # Inverse text normalisation turns spoken numbers and dates into digits,
        # which is what the language model expects to see.
        self._use_itn = use_itn
        self._loop: asyncio.AbstractEventLoop | None = None
        self._turn_id: UUID | None = None
        self._recognizer = None
        self._buffer = _TurnBuffer(sample_rate)
        self._results: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=50)
        # The most recent partial, so returning to silence after a partial does
        # not re-announce the same words as if they were new.
        self._last_partial = ""
        self._last_decode_at = 0.0
        self._decoded_seconds = 0.0
        self._lock = asyncio.Lock()
        # Latency accounting, so the effect of a change is measurable on the Pi
        # and comparable with the cloud backend's counters.
        self.last_load_ms = 0.0
        self.last_decode_ms = 0.0
        self.decodes = 0
        self.partials = 0

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Load the model, off the event loop.

        This is the expensive step and it happens once. A failure here is a
        configuration mistake with a fix the user can act on, so it is raised
        rather than logged: a recogniser that cannot load anything would
        otherwise look like a microphone that hears nothing.
        """
        self._loop = asyncio.get_running_loop()
        available, reason = self._availability()
        if not available:
            raise RuntimeError(f"Local speech recognition is not usable: {reason}")
        started = time.monotonic()
        # The load is blocking and takes seconds, so it must not run on the loop;
        # a timeout means the thread is left to finish on its own rather than
        # starting the service with no recogniser at all.
        try:
            async with asyncio.timeout(MODEL_LOAD_TIMEOUT_SECONDS):
                await asyncio.to_thread(self._load)
        except TimeoutError:
            raise RuntimeError(
                "Loading the local speech model took too long. "
                f"Check that {self._model_file} is a complete download.") from None
        self.last_load_ms = (time.monotonic() - started) * 1000

    def _availability(self) -> tuple[bool, str]:
        """Check this instance, not the environment.

        The tests pass explicit paths, and a user may too, so the configured
        defaults are only the fallback.
        """
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False, "sherpa-onnx is not installed (pip install sherpa-onnx)"
        if not self._model_file.is_file() or self._model_file.stat().st_size == 0:
            return False, f"the model {self._model_file} is not there"
        if not self._tokens_file.is_file():
            return False, f"the tokens file {self._tokens_file} is not there"
        return True, ""

    def _load(self) -> None:
        import sherpa_onnx

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self._model_file),
            tokens=str(self._tokens_file),
            num_threads=self._threads,
            use_itn=self._use_itn,
            language=self._language,
            debug=False,
        )

    async def close(self) -> None:
        self._recognizer = None
        self._buffer.clear()

    # -- one turn ----------------------------------------------------------

    async def start_turn(self, turn_id: UUID) -> None:
        self._turn_id = turn_id
        self._buffer.clear()
        self._last_partial = ""
        self._last_decode_at = 0.0
        self._decoded_seconds = 0.0
        while not self._results.empty():
            self._results.get_nowait()

    async def send_audio(self, pcm: bytes) -> None:
        """Buffer the packet, and decode when there is something new to say.

        The threshold is what keeps this affordable: decoding every 100 ms
        packet would run the model ten times a second to produce the same
        partial ten times. Decoding every 800 ms produces the visible update at
        a tenth of the CPU cost.
        """
        if self._recognizer is None or self._turn_id is None:
            return
        self._buffer.append(pcm)
        if self._buffer.seconds < MINIMUM_DECODE_SECONDS:
            return
        if self._buffer.seconds - self._decoded_seconds < PARTIAL_INTERVAL_SECONDS:
            return
        await self._decode(final=False)

    async def _decode(self, final: bool) -> str:
        """Run the model over the turn's audio and publish what it heard.

        One decode runs at a time. Audio keeps arriving while the worker thread
        is busy, and it lands in the buffer that the next decode reads, so a
        decode that is overtaken by new speech is not wasted — the newer audio is
        already included in whichever decode runs next.
        """
        async with self._lock:
            turn = self._turn_id
            if self._recognizer is None or turn is None:
                return ""
            seconds = self._buffer.seconds
            samples = self._buffer.samples()
            self._decoded_seconds = seconds
            started = time.monotonic()
            try:
                async with asyncio.timeout(DECODE_TIMEOUT_SECONDS):
                    text = await asyncio.to_thread(self._transcribe, samples)
            except TimeoutError:
                # Not fatal: the audio is still buffered, so a later decode
                # includes it. Reporting a failure here would end a turn that is
                # still perfectly recognisable.
                print("Local speech recognition did not finish in time; skipping "
                      "this partial.", flush=True)
                return ""
            except Exception as error:
                self._publish(Transcript(turn, f"[STT error] {error}", True, 0.0))
                return ""
            self.last_decode_ms = (time.monotonic() - started) * 1000
            self.decodes += 1
        return self._publish_text(turn, text, final)

    def _transcribe(self, samples: list[float]) -> str:
        """One blocking decode on a worker thread."""
        stream = self._recognizer.create_stream()
        stream.accept_waveform(self._sample_rate, samples)
        self._recognizer.decode_stream(stream)
        return strip_tags(stream.result.text)

    def _publish_text(self, turn: UUID, text: str, final: bool) -> str:
        if final:
            self._publish(Transcript(turn, text or "", True, None))
            return text
        # Partials replace each other rather than accumulate, matching the cloud
        # backend: only the newest text describes what was said. Re-publishing an
        # unchanged partial would print the same line twice.
        if text and text != self._last_partial:
            self._last_partial = text
            self.partials += 1
            self._publish(Transcript(turn, text, False, None))
        return text

    async def results(self):
        while True:
            yield await self._results.get()

    async def finish_turn(self) -> None:
        """Decode once more against the whole utterance, then close the turn.

        The final decode is deliberately unconditional once there is audio: the
        last words of a sentence usually arrive after the last partial, so the
        partials are what the user sees while speaking and this is what the
        language model is actually given.

        The `[STT complete]` sentinel is always published, including when nothing
        was heard. The coordinator keys off that string to end listening, so a
        silent turn that published nothing would sit waiting forever.
        """
        turn = self._turn_id
        if turn is None:
            return
        if self._recognizer is not None and self._buffer.seconds >= MINIMUM_DECODE_SECONDS:
            await self._decode(final=True)
        # A short/noisy segment can complete without any text; this is the same
        # sentinel the cloud backend's on_complete produces.
        self._publish(Transcript(turn, "[STT complete]", True, 0.0))
        self._turn_id = None
        self._buffer.clear()
    def _publish(self, transcript: Transcript) -> None:
        """Hand a transcript to the event loop from whichever thread produced it.

        Same contract as the cloud recogniser's: the queue is bounded and the
        oldest entry is dropped when full, because a consumer that has stopped
        reading must not be able to grow memory without limit. Everything here
        runs on the loop already, but call_soon_threadsafe is kept so the method
        stays correct if a future path calls it from a worker.
        """
        if self._loop is None:
            return

        def put() -> None:
            if self._results.full():
                self._results.get_nowait()
            self._results.put_nowait(transcript)

        if self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(put)
        except RuntimeError:
            pass
