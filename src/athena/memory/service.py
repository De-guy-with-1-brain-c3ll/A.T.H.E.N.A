from __future__ import annotations

import asyncio
from collections import deque
import json
from pathlib import Path
import re
from uuid import UUID

from openai import AsyncOpenAI

from athena.memory.apply import apply_result
from athena.memory.database import MemoryDatabase, StoredTurn
from athena.memory.quality import SENSITIVE_PATTERN as _SENSITIVE
from athena.settings.store import RuntimeSettingsStore
from athena.prompts import read_prompt


_SECRET_VALUE = re.compile(
    r"\bsk-[A-Za-z0-9._-]{12,}\b|"
    r"\b(?:api[_ -]?key|app[_ -]?secret|access[_ -]?token|password)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


# How long a turn will wait for the memory queue before giving up on it.
MEMORY_ENQUEUE_TIMEOUT_SECONDS = 2.0


class MemoryService:
    """Fast local context plus a separate, asynchronous memory concentrator."""

    def __init__(
        self,
        database_path: Path,
        api_key: str,
        model: str,
        batch_delay_seconds: float = 1.5,
        settings: RuntimeSettingsStore | None = None,
    ) -> None:
        self._database = MemoryDatabase(database_path)
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            timeout=30.0,
            max_retries=1,
        )
        self._model = model
        self._batch_delay = batch_delay_seconds
        self._settings = settings
        self._queue: asyncio.Queue[StoredTurn] = asyncio.Queue(maxsize=32)
        self._recent: deque[StoredTurn] = deque(maxlen=8)
        self._summary = ""
        self._facts: list[tuple[str, str, float]] = []
        self._worker_task: asyncio.Task | None = None
        self._concentration_calls = 0
        # Turns the queue was too busy to accept. Counted so the loss is
        # visible rather than silent.
        self.dropped_turns = 0
        self._prompt = read_prompt("memory")

    async def connect(self) -> None:
        await asyncio.to_thread(self._database.initialize)
        recent, summary, facts = await asyncio.gather(
            asyncio.to_thread(self._database.recent_turns, 8),
            asyncio.to_thread(self._database.get_summary),
            asyncio.to_thread(self._database.facts, 20),
        )
        # Never put credential-like values back into a model prompt, including
        # values saved by an older ATHENA version.
        self._recent.extend(turn for turn in recent if not (
            _SECRET_VALUE.search(turn.user_text) or _SECRET_VALUE.search(turn.assistant_text)
        ))
        self._summary = summary
        self._facts = facts
        self._worker_task = asyncio.create_task(self._worker())

    def context_messages(self, *, max_turns: int = 8, max_chars: int = 8_000) -> list[dict[str, str]]:
        if self._settings is not None and not self._settings.get("memory_enabled"):
            return []
        messages: list[dict[str, str]] = []
        memory_parts = []
        if self._summary:
            memory_parts.append("Rolling summary:\n" + self._summary)
        if self._facts:
            fact_text = "\n".join(
                f"- {key}: {value} (confidence {confidence:.2f})"
                for key, value, confidence in self._facts[:8]
            )
            memory_parts.append("Durable facts:\n" + fact_text)
        if memory_parts:
            messages.append(
                {"role": "system", "content": "Relevant memory:\n" + "\n\n".join(memory_parts)}
            )
        # Keep voice prompts small. Recent turns are useful for continuity but
        # old transcripts quickly dominate input-token cost.
        recent = list(self._recent)[-max(0, int(max_turns)):]
        used = sum(len(message["content"]) for message in messages)
        for turn in recent:
            remaining = max(0, int(max_chars) - used)
            if remaining < 40:
                break
            user = turn.user_text[: max(20, remaining // 2)]
            assistant = turn.assistant_text[: max(20, remaining - len(user))]
            messages.extend(
                (
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": assistant},
                )
            )
            used += len(user) + len(assistant)
        return messages

    async def remember_turn(
        self, turn_id: UUID, user_text: str, assistant_text: str
    ) -> None:
        if self._settings is not None and not self._settings.get("memory_enabled"):
            return
        if _SECRET_VALUE.search(user_text) or _SECRET_VALUE.search(assistant_text):
            return
        turn = StoredTurn(turn_id, user_text, assistant_text)
        self._recent.append(turn)
        try:
            # Never let a backed-up memory worker block the conversation. If the
            # queue is full the turn is already in _recent, so this turn still has
            # its context; only the durable write is skipped.
            async with asyncio.timeout(MEMORY_ENQUEUE_TIMEOUT_SECONDS):
                await self._queue.put(turn)
        except TimeoutError:
            self.dropped_turns += 1
            print(f"Memory is behind; not persisting turn {str(turn_id)[:8]}.",
                  flush=True)

    async def _save_batch(self, batch: list[StoredTurn]) -> None:
        """Persist turns, retrying the transient lock contention two processes cause.

        The voice service and the dashboard share one SQLite file, so a write can
        lose the race for the lock. The old code gave up on the first failure and
        dropped the turns, silently losing real conversation history.
        """
        for attempt in range(3):
            try:
                for turn in batch:
                    await asyncio.to_thread(self._database.save_turn, turn)
                return
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))

    async def _worker(self) -> None:
        pending_summary: list[StoredTurn] = []
        while True:
            first = await self._queue.get()
            batch = [first]
            try:
                await asyncio.sleep(self._batch_delay)
                while len(batch) < 6:
                    batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                pass
            try:
                await self._save_batch(batch)
                pending_summary.extend(batch)
                batch_size = (self._settings.get("memory_summary_batch_size")
                              if self._settings is not None else 6)
                while len(pending_summary) >= batch_size:
                    summary_batch = pending_summary[:batch_size]
                    del pending_summary[:batch_size]
                    await self._concentrate(summary_batch)
            except Exception as error:
                print(f"Memory update deferred: {error}")
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _concentrate(self, turns: list[StoredTurn]) -> None:
        self._concentration_calls += 1
        conversation = "\n\n".join(
            f"TURN {turn.turn_id}\nUSER: {turn.user_text}\nATHENA: {turn.assistant_text}"
            for turn in turns
        )
        existing_facts = "\n".join(f"{k}: {v}" for k, v, _ in self._facts)
        messages = [
            {"role": "system", "content": self._prompt},
            {
                "role": "user",
                "content": (
                    f"EXISTING SUMMARY:\n{self._summary or '(none)'}\n\n"
                    f"EXISTING FACTS:\n{existing_facts or '(none)'}\n\n"
                    f"NEW COMPLETED TURNS:\n{conversation}"
                ),
            },
        ]
        result = None
        last_error: Exception | None = None
        for attempt in range(2):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.0,
                max_tokens=600,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            raw = (response.choices[0].message.content or "").strip()
            try:
                if not raw:
                    raise ValueError("memory model returned empty JSON")
                result = json.loads(raw)
                break
            except (json.JSONDecodeError, ValueError) as error:
                last_error = error
                if attempt == 0:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Return a smaller valid JSON object now. Use at most four changed facts.",
                        }
                    )
        if result is None:
            raise ValueError(f"memory model returned invalid JSON twice: {last_error}")
        # The same gate sleep mode uses, in one place: sensitive data, the quality
        # rules, and the cap on how large the table may grow.
        applied = await apply_result(self._database, result, turns[-1].turn_id)
        if applied.summary_chars:
            self._summary = await asyncio.to_thread(self._database.get_summary)
        self._facts = await asyncio.to_thread(self._database.facts, 20)

    async def close(self) -> None:
        await self._queue.join()
        if self._worker_task is not None:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
        await self._client.close()
