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

# Let the last audio frames arrive before the socket goes away.
SESSION_CLOSE_GRACE_SECONDS = 1.5

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
        # A realtime session can be opened and configured before there is text
        # to speak.  Keeping that handshake off the first-answer path is the
        # only meaningful latency win available to this backend.
        self._session_lock = asyncio.Lock()
        self._session_epoch = 0

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

    async def warm(self) -> bool:
        """Open and configure an idle session for the next reply.

        The coordinator calls this in the background after startup.  Unlike a
        throwaway connectivity check, this exact socket is adopted by the first
        turn, so DNS/TLS/session configuration are not part of time-to-speech.
        A failed warm-up is deliberately harmless: normal synthesis will retry.
        """
        try:
            await self._ensure_session()
            return True
        except Exception:
            return False

    async def _start(self, turn_id: UUID) -> None:
        async with self._session_lock:
            if self._session is not None and self._turn_id in {None, turn_id}:
                self._turn_id = turn_id
                return
        # Do not reuse a session that has already served another response.
        if self._session is not None:
            await self.cancel(self._turn_id)  # type: ignore[arg-type]
        async with self._session_lock:
            await self._ensure_session_locked()
            self._turn_id = turn_id

    async def _ensure_session(self) -> None:
        async with self._session_lock:
            await self._ensure_session_locked()

    async def _ensure_session_locked(self) -> None:
        """Create the idle session while the lifecycle lock is held."""
        if self._session is not None:
            return
        self._session_epoch += 1
        epoch = self._session_epoch
        owner = self

        class Callback(QwenTtsRealtimeCallback):
            ended = False

            def on_open(self) -> None:
                pass

            def on_close(self, close_status_code, close_msg) -> None:
                self._end_turn()

            def on_event(self, response: dict) -> None:
                # A close callback from a retired socket must never end a newer
                # turn.  The SDK invokes callbacks from its own worker thread.
                if owner._session_epoch != epoch or owner._turn_id is None:
                    return
                event_type = response.get("type")
                if event_type == "response.audio.delta":
                    owner._publish(
                        AudioChunk(owner._turn_id, base64.b64decode(response["delta"]))
                    )
                elif event_type == "session.finished":
                    self._end_turn()

            def _end_turn(self) -> None:
                if self.ended:
                    return
                self.ended = True
                # The socket has to be closed, not just forgotten. Setting
                # _session to None left a realtime session open on DashScope's
                # side after every single turn, and a realtime session is billed
                # for as long as it stays open. That is what made speech, rather
                # than conversation, the overwhelming majority of the bill.
                if owner._session_epoch != epoch or owner._turn_id is None:
                    return
                owner._retire(epoch, owner._turn_id)

        session = QwenTtsRealtime(
            model=self._model,
            callback=Callback(),
            url="wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
        )
        self._session = session
        try:
            await asyncio.to_thread(session.connect)
            # Session configuration is another blocking socket write; keep it off
            # the event loop so the first spoken clause is not delayed behind it.
            await asyncio.to_thread(
                session.update_session,
                voice=self._voice,
                response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                mode="server_commit",
                speech_rate=self._settings.get("tts_speech_rate") if self._settings else 1.2,
            )
        except Exception:
            # A failed background warm-up must not leave a poisoned session that
            # prevents the real turn from retrying.
            if self._session is session:
                self._session = None
                self._session_epoch += 1
            try:
                await asyncio.to_thread(session.close)
            except Exception:
                pass
            raise

    def _retire(self, epoch: int | None = None, turn_id: UUID | None = None) -> None:
        """Close the current session for real, off the event loop.

        Called when a turn's audio has finished. The websocket is closed rather
        than dropped, so nothing is left open and billing after the reply ends.

        The close is deliberately delayed by a moment. Closing the instant the
        server says it has finished can race the last audio deltas still arriving
        on the socket, which drops the end of the reply — the audio stopping
        mid-sentence while the log shows the whole answer. A short grace period
        costs nothing and lets those frames land first.
        """
        # Optional arguments retain the small private lifecycle helper used by
        # diagnostics and older callers; callbacks pass explicit values to
        # protect against a stale socket ending a new turn.
        epoch = self._session_epoch if epoch is None else epoch
        turn_id = self._turn_id if turn_id is None else turn_id
        if self._session_epoch != epoch:
            return
        session, self._session = self._session, None
        self._turn_id = None
        self._session_epoch += 1
        if session is None or self._loop is None or self._loop.is_closed():
            return

        async def shut() -> None:
            try:
                await asyncio.sleep(SESSION_CLOSE_GRACE_SECONDS)
                await asyncio.to_thread(session.close)
            except Exception:
                # A session that is already gone is the desired end state.
                pass

        try:
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(shut()))
        except RuntimeError:
            pass
        if turn_id is not None:
            self._publish(turn_id)

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
        # Opening the socket is the cost, so never open one for nothing.
        if not text or not text.strip():
            return
        await self._start(turn_id)
        session = self._session
        if session is not None:
            # append_text is a blocking websocket write on the first-audio path.
            await asyncio.to_thread(session.append_text, text)

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
        async with self._session_lock:
            if self._session is None or self._turn_id != turn_id:
                return
            session, self._session = self._session, None
            self._turn_id = None
            self._session_epoch += 1
        await asyncio.to_thread(session.cancel_response)
        await asyncio.to_thread(session.close)
        self._publish(turn_id)

    async def close(self) -> None:
        async with self._session_lock:
            if self._session is None:
                return
            session, self._session = self._session, None
            self._turn_id = None
            self._session_epoch += 1
        if session is not None:
            await asyncio.to_thread(session.close)
