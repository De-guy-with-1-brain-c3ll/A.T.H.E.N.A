from __future__ import annotations

import asyncio
from uuid import UUID

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from athena.events import Transcript


class FunAsrRecognizer:
    """DashScope streaming recognizer shared by Fun-ASR and Qwen-Audio-ASR."""

    def __init__(
        self, api_key: str, model: str, sample_rate: int, language: str = "en"
    ) -> None:
        if model.startswith("fun-asr-flash-8k-realtime") and language != "zh":
            raise ValueError("The 8k Fun-ASR model supports Chinese only; use fun-asr-realtime for English.")
        self._api_key = api_key
        self._model = model
        self._sample_rate = sample_rate
        self._language = language
        self._loop: asyncio.AbstractEventLoop | None = None
        self._turn_id: UUID | None = None
        self._recognition: Recognition | None = None
        self._results: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=50)

    async def connect(self) -> None:
        dashscope.api_key = self._api_key
        dashscope.base_websocket_api_url = (
            "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
        )
        self._loop = asyncio.get_running_loop()

    async def start_turn(self, turn_id: UUID) -> None:
        await self.finish_turn()
        while not self._results.empty():
            self._results.get_nowait()
        self._turn_id = turn_id
        owner = self

        class Callback(RecognitionCallback):
            def on_open(self) -> None:
                pass

            def on_close(self) -> None:
                pass

            def on_complete(self) -> None:
                # A short/noisy segment may complete without any final text.
                # Wake the coordinator so it can start a fresh listening turn.
                owner._publish(Transcript(turn_id, "[STT complete]", True, 0.0))

            def on_error(self, result: RecognitionResult) -> None:
                owner._publish(
                    Transcript(turn_id, f"[STT error] {result.message}", True, 0.0)
                )

            def on_event(self, result: RecognitionResult) -> None:
                sentence = result.get_sentence() or {}
                text = sentence.get("text", "").strip()
                if not text:
                    return
                confidence = sentence.get("confidence")
                owner._publish(
                    Transcript(
                        turn_id=turn_id,
                        text=text,
                        is_final=RecognitionResult.is_sentence_end(sentence),
                        confidence=float(confidence) if confidence is not None else None,
                    )
                )

        self._recognition = Recognition(
            model=self._model,
            format="pcm",
            sample_rate=self._sample_rate,
            semantic_punctuation_enabled=True,
            language_hints=[self._language],
            callback=Callback(),
        )
        start = asyncio.create_task(asyncio.to_thread(self._recognition.start))
        try:
            await asyncio.shield(start)
        except asyncio.CancelledError:
            # The SDK runs in a thread. Don't stop/reuse this recognizer while its
            # connection is still being opened by that thread.
            await asyncio.gather(start, return_exceptions=True)
            raise

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
        if self._recognition is not None:
            try:
                self._recognition.send_audio_frame(pcm)
            except Exception as error:
                if "stopped" not in str(error).casefold():
                    raise
                turn_id, self._recognition = self._turn_id, None
                if turn_id is not None:
                    self._publish(Transcript(turn_id, "[STT error] connection closed", True, 0.0))

    async def results(self):
        while True:
            yield await self._results.get()

    async def finish_turn(self) -> None:
        recognition, self._recognition = self._recognition, None
        if recognition is not None:
            stop = asyncio.create_task(asyncio.to_thread(recognition.stop))
            try:
                await asyncio.shield(stop)
            except asyncio.CancelledError:
                await asyncio.gather(stop, return_exceptions=True)
                raise
            except Exception as error:
                # The service can close an idle turn before our local VAD does.
                # Stopping an already-stopped SDK object is harmless.
                if "stopped" not in str(error).casefold():
                    raise

    async def close(self) -> None:
        await self.finish_turn()
