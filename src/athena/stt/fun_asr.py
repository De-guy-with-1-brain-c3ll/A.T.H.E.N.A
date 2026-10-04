from __future__ import annotations

import asyncio
import os
import time
from uuid import UUID

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from athena.events import Transcript


# Opening the DashScope socket costs a DNS lookup, a TCP and TLS handshake and a
# websocket upgrade. Doing that after the user has already started talking put
# that whole cost on the critical path of every utterance, so a session is kept
# ready during the silence between turns instead.
PREWARM_TTL_SECONDS = 30.0
# The DashScope SDK blocks on its own socket. Every call into it is bounded, so a
# wedged connection costs one turn instead of the whole service: an unbounded
# stop() left the listen task unfinished and the run loop waiting forever, with
# no listening, no speech and no error to explain it.
SDK_CALL_TIMEOUT_SECONDS = 10.0
# Alibaba recommends roughly 100 ms of audio per streaming packet.
PACKET_BYTES = 3200


class FunAsrRecognizer:
    """DashScope streaming recognizer shared by Fun-ASR and Qwen-Audio-ASR."""

    def __init__(
        self, api_key: str, model: str, sample_rate: int, language: str = "en",
        prewarm: bool = True, max_sentence_silence_ms: int | None = None,
        semantic_punctuation: bool = False,
    ) -> None:
        if model.startswith("fun-asr-flash-8k-realtime") and language != "zh":
            raise ValueError("The 8k Fun-ASR model supports Chinese only; use fun-asr-realtime for English.")
        self._api_key = api_key
        self._model = model
        self._sample_rate = sample_rate
        self._language = language
        # The configured language may itself be a list ("en,zh"), which is what
        # mixed English and Chinese speech needs.
        configured = os.environ.get("ATHENA_STT_LANGUAGES", "").strip() or language
        self._languages = [part.strip() for part in configured.split(",") if part.strip()] or ["en"]
        self._vocabulary_id = os.environ.get("ATHENA_STT_VOCABULARY_ID", "").strip()
        self._prewarm_enabled = prewarm
        self._max_sentence_silence_ms = max_sentence_silence_ms
        # Semantic punctuation decides sentence ends from grammar, ignoring
        # pauses. That delays finals until the service is satisfied, so a
        # command answered then is already a turn behind the conversation.
        self._semantic_punctuation = semantic_punctuation
        self._loop: asyncio.AbstractEventLoop | None = None
        self._turn_id: UUID | None = None
        self._recognition: Recognition | None = None
        # A callback belongs to one websocket session.  It deliberately owns a
        # turn id instead of looking at ``self._turn_id`` when an event arrives:
        # DashScope can deliver the final event after stop(), by which time a new
        # user utterance may already have started.
        self._callback = None
        self._prewarmed = False
        self._prewarm_started_at = 0.0
        self._prewarm_task: asyncio.Task | None = None
        self._results: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=50)
        # Latency accounting, so the effect of a change is measurable on the Pi.
        self.last_connect_ms = 0.0
        self.last_silence_ms = 0
        self.reused_sessions = 0
        self.reconnects = 0

    async def connect(self) -> None:
        dashscope.api_key = self._api_key
        dashscope.base_websocket_api_url = (
            "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
        )
        self._loop = asyncio.get_running_loop()
        if self._prewarm_enabled:
            await self._prewarm()

    def _session_kwargs(self) -> dict:
        kwargs = {
            "model": self._model,
            "format": "pcm",
            "sample_rate": self._sample_rate,
            "semantic_punctuation_enabled": getattr(
                self, "_semantic_punctuation", False),
            # A single hint made the recogniser guess at the Chinese words mixed
            # into English sentences, which is where "CJ" became "Jesus" and
            # "siege" became "sieges". More than one hint is allowed.
            "language_hints": list(self._languages) or [self._language],
        }
        if self._vocabulary_id:
            # A hot-word list is the supported way to teach it domain words like
            # CJ, 9Gd and CodeHS that no general model will know.
            kwargs["vocabulary_id"] = self._vocabulary_id
        if self._max_sentence_silence_ms is not None:
            # Ask the service to finalise a sentence when our own gate does,
            # instead of waiting for the server's longer default.
            kwargs["max_sentence_silence"] = int(self._max_sentence_silence_ms)
        return kwargs

    def _make_callback(self, turn_id: UUID | None = None) -> RecognitionCallback:
        owner = self

        class Callback(RecognitionCallback):
            """Reports only against the turn that owns this websocket session."""

            def __init__(self, assigned_turn: UUID | None) -> None:
                self.turn_id = assigned_turn

            def _turn(self) -> UUID:
                # A pre-warmed session has no owner until it is reused.  Its
                # unexpected close/error is harmless and must not leak into a
                # later real turn.
                return self.turn_id or UUID(int=0)

            def on_open(self) -> None:
                pass

            def on_close(self) -> None:
                pass

            def on_complete(self) -> None:
                # A short/noisy segment may complete without any final text.
                # Wake the coordinator so it can start a fresh listening turn.
                owner._publish(Transcript(self._turn(), "[STT complete]", True, 0.0))

            def on_error(self, result: RecognitionResult) -> None:
                owner._publish(
                    Transcript(self._turn(), f"[STT error] {result.message}", True, 0.0)
                )

            def on_event(self, result: RecognitionResult) -> None:
                sentence = result.get_sentence() or {}
                text = sentence.get("text", "").strip()
                if not text:
                    return
                confidence = sentence.get("confidence")
                owner._publish(
                    Transcript(
                        turn_id=self._turn(),
                        text=text,
                        is_final=RecognitionResult.is_sentence_end(sentence),
                        confidence=float(confidence) if confidence is not None else None,
                    )
                )

        return Callback(turn_id)

    async def _open(self, turn_id: UUID | None = None) -> None:
        callback = self._make_callback(turn_id)
        recognition = Recognition(callback=callback, **self._session_kwargs())
        started = time.monotonic()
        try:
            async with asyncio.timeout(SDK_CALL_TIMEOUT_SECONDS):
                await asyncio.to_thread(recognition.start)
        except TimeoutError:
            raise RuntimeError(
                "The speech service did not answer in time. Trying again.") from None
        self.last_connect_ms = (time.monotonic() - started) * 1000
        self._recognition = recognition
        self._callback = callback

    async def _prewarm(self) -> None:
        """Hold a session open so the next utterance skips the handshake.

        No audio is sent, and DashScope bills per second of audio received, so a
        waiting session costs nothing. It is dropped after a short TTL because an
        idle websocket is eventually closed by the service.
        """
        if not self._prewarm_enabled or self._recognition is not None:
            return
        try:
            await self._open()
        except Exception as error:
            # Pre-warming is an optimisation; a failure must not stop anything.
            print(f"Speech recognition pre-warm failed: {error}", flush=True)
            self._recognition = None
            return
        self._prewarmed = True
        self._prewarm_started_at = time.monotonic()

    async def start_turn(self, turn_id: UUID) -> None:
        self._turn_id = turn_id
        # finish_turn starts a pre-warm asynchronously.  A new utterance can
        # arrive while that socket is opening; cancel and join it before opening
        # a foreground socket, otherwise the late pre-warm overwrites
        # ``self._recognition`` and sends this turn to the wrong session.
        prewarm_task = self._prewarm_task
        if prewarm_task is not None:
            prewarm_task.cancel()
            await asyncio.gather(prewarm_task, return_exceptions=True)
            if self._prewarm_task is prewarm_task:
                self._prewarm_task = None
        if self._recognition is not None and self._prewarmed:
            if time.monotonic() - self._prewarm_started_at <= PREWARM_TTL_SECONDS:
                # Reuse the waiting session: this is the latency win.
                self._prewarmed = False
                self._callback.turn_id = turn_id
                self.reused_sessions += 1
                return
            await self._discard()
        else:
            await self._finish_turn(prewarm=False)
        while not self._results.empty():
            self._results.get_nowait()
        start = asyncio.create_task(self._open(turn_id))
        try:
            await asyncio.shield(start)
        except asyncio.CancelledError:
            # The SDK runs in a thread. Don't stop/reuse this recognizer while its
            # connection is still being opened by that thread.
            await asyncio.gather(start, return_exceptions=True)
            raise

    async def _discard(self) -> None:
        recognition, self._recognition = self._recognition, None
        self._prewarmed = False
        if recognition is not None:
            await asyncio.gather(asyncio.to_thread(recognition.stop),
                                 return_exceptions=True)

    def _publish(self, transcript: Transcript) -> None:
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

    async def send_audio(self, pcm: bytes) -> None:
        recognition = self._recognition
        if recognition is None:
            return
        try:
            # The SDK writes to a blocking websocket. Running that on the event
            # loop stalls microphone capture and playback for the whole send, so
            # it goes to a worker thread. Frames are still sent in order because
            # each call is awaited before the next one starts.
            await asyncio.to_thread(recognition.send_audio_frame, pcm)
            self._meter_bytes = getattr(self, '_meter_bytes', 0) + len(pcm)
        except Exception as error:
            if "stopped" not in str(error).casefold():
                raise
            turn_id = self._turn_id
            self._recognition = None
            if turn_id is None or self._prewarmed:
                self._prewarmed = False
                self._publish(Transcript(turn_id or UUID(int=0),
                                         "[STT error] connection closed", True, 0.0))
                return
            # A pre-warmed session can have been closed by the service while it
            # waited. Reopen once and resend, so the utterance is not lost.
            self.reconnects += 1
            try:
                await self._open(turn_id)
                await asyncio.to_thread(self._recognition.send_audio_frame, pcm)
                self._meter_bytes = getattr(self, '_meter_bytes', 0) + len(pcm)
            except Exception:
                self._recognition = None
                self._publish(Transcript(turn_id, "[STT error] connection closed", True, 0.0))

    async def results(self):
        while True:
            yield await self._results.get()

    async def _finish_turn(self, *, prewarm: bool) -> None:
        sent = getattr(self, '_meter_bytes', 0)
        self._meter_bytes = 0
        if sent:
            from athena.metrics import record
            await asyncio.to_thread(record, 'qwen_stt', {
                'audio_seconds': sent / (self._sample_rate * 2),
                'audio_bytes': sent,
                'token_estimate': 'unavailable: audio duration is measured, not text tokens'})
        recognition, self._recognition = self._recognition, None
        self._callback = None
        self._prewarmed = False
        if recognition is not None:
            stop = asyncio.create_task(asyncio.to_thread(recognition.stop))
            try:
                async with asyncio.timeout(SDK_CALL_TIMEOUT_SECONDS):
                    await asyncio.shield(stop)
            except TimeoutError:
                # The shield keeps the blocking call running in its own thread.
                # Leaking that thread is the lesser evil: waiting on it would
                # freeze the assistant with nothing to show the user.
                print("Speech recognition did not stop in time; leaving that session.",
                      flush=True)
            except asyncio.CancelledError:
                await asyncio.gather(stop, return_exceptions=True)
                raise
            except Exception as error:
                # The service can close an idle turn before our local VAD does.
                # Stopping an already-stopped SDK object is harmless.
                if "stopped" not in str(error).casefold():
                    raise
        # Get the next session ready while the answer is being produced, so the
        # handshake is off the critical path of the following utterance.
        if prewarm and self._prewarm_enabled and self._prewarm_task is None:
            self._prewarm_task = asyncio.create_task(self._prewarm())
            def finished(task):
                if self._prewarm_task is task:
                    self._prewarm_task = None
            self._prewarm_task.add_done_callback(finished)

    async def finish_turn(self) -> None:
        await self._finish_turn(prewarm=True)

    async def close(self) -> None:
        self._prewarm_enabled = False
        if self._prewarm_task is not None:
            self._prewarm_task.cancel()
            await asyncio.gather(self._prewarm_task, return_exceptions=True)
            self._prewarm_task = None
        await self.finish_turn()
