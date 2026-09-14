from __future__ import annotations

import asyncio
import base64
from uuid import UUID

import dashscope
from dashscope.audio.qwen_tts_realtime import (
    AudioFormat,
    QwenTtsRealtime,
    QwenTtsRealtimeCallback,
)

from athena.events import AudioChunk


class QwenRealtimeSynthesizer:
    def __init__(self, api_key: str, model: str, voice: str, settings=None) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._settings = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: QwenTtsRealtime | None = None
        self._turn_id: UUID | None = None
        # Qwen can briefly synthesize faster than the speaker consumes PCM.
        # Audio must never be dropped: doing so removes words from the reply.
        # Stale turns are rejected by turn_id in the coordinator instead.
        self._audio: asyncio.Queue[AudioChunk | UUID] = asyncio.Queue()

    async def cache_phrase(self, text: str) -> bytes:
        """Use a separate session so a failed warm-up cannot poison live audio."""
        from uuid import uuid4
        cache = QwenRealtimeSynthesizer(self._api_key, self._model, self._voice, self._settings)
        turn = uuid4()
        try:
            await cache.connect()
            await cache.send_text(turn, text)
            await cache.flush(turn)
            return b"".join([chunk.pcm async for chunk in cache.audio(turn)])
        finally:
            await cache.close()

    async def connect(self) -> None:
        dashscope.api_key = self._api_key
        self._loop = asyncio.get_running_loop()

    async def _start(self, turn_id: UUID) -> None:
        if self._session is not None and self._turn_id == turn_id:
            return
        if self._session is not None:
            await self.cancel(self._turn_id)  # type: ignore[arg-type]
        self._turn_id = turn_id
        owner = self

        class Callback(QwenTtsRealtimeCallback):
            ended = False

            def on_open(self) -> None:
                pass

            def on_close(self, close_status_code, close_msg) -> None:
                self._end_turn()

            def on_event(self, response: dict) -> None:
                event_type = response.get("type")
                if event_type == "response.audio.delta":
                    owner._publish(
                        AudioChunk(turn_id, base64.b64decode(response["delta"]))
                    )
                elif event_type == "session.finished":
                    self._end_turn()

            def _end_turn(self) -> None:
                if self.ended:
                    return
                self.ended = True
                if owner._turn_id == turn_id:
                    owner._session = None
                owner._publish(turn_id)

        session = QwenTtsRealtime(
            model=self._model,
            callback=Callback(),
            url="wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
        )
        self._session = session
        await asyncio.to_thread(session.connect)
        session.update_session(
            voice=self._voice,
            response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            mode="server_commit",
            speech_rate=self._settings.get("tts_speech_rate") if self._settings else 1.2,
        )

    def _publish(self, item: AudioChunk | UUID) -> None:
        if self._loop is None:
            return

        def put() -> None:
            self._audio.put_nowait(item)

        if not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(put)
            except RuntimeError:
                pass

    async def send_text(self, turn_id: UUID, text: str) -> None:
        await self._start(turn_id)
        if self._session is not None:
            self._session.append_text(text)

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
        if self._session is not None and self._turn_id == turn_id:
            await asyncio.to_thread(self._session.finish)

    async def cancel(self, turn_id: UUID) -> None:
        if self._session is None or self._turn_id != turn_id:
            return
        session, self._session = self._session, None
        await asyncio.to_thread(session.cancel_response)
        await asyncio.to_thread(session.close)
        self._publish(turn_id)

    async def close(self) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            await asyncio.to_thread(session.close)
