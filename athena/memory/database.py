from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class StoredTurn:
    turn_id: UUID
    user_text: str
    assistant_text: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True, slots=True)
class ConversationRow:
    turn_id: UUID
    started_at: str
    user_text: str
    assistant_text: str


class MemoryDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    user_text TEXT,
                    assistant_text TEXT,
                    status TEXT NOT NULL,
                    first_audio_ms REAL,
                    total_ms REAL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source_turn_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def save_turn(self, turn: StoredTurn) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT OR REPLACE INTO turns
                   (id, started_at, user_text, assistant_text, status)
                   VALUES (?, ?, ?, ?, 'completed')""",
                (str(turn.turn_id), turn.started_at, turn.user_text, turn.assistant_text),
            )
            connection.commit()

    def recent_turns(self, limit: int = 10) -> list[StoredTurn]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, user_text, assistant_text, started_at FROM turns
                   WHERE status = 'completed' AND started_at > COALESCE(
                       (SELECT value FROM memory_state WHERE key='context_cleared_at'), '')
                   ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        rows.reverse()
        return [StoredTurn(UUID(row[0]), row[1] or "", row[2] or "", row[3]) for row in rows]

    def turns_between(self, start: datetime, end: datetime) -> list[StoredTurn]:
        """Completed turns inside a window, oldest first.

        Sleep mode consolidates a whole day at once, so it needs the day's turns
        in order rather than the most recent handful.
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, user_text, assistant_text, started_at FROM turns
                   WHERE status = 'completed' AND started_at >= ? AND started_at < ?
                   AND started_at > COALESCE((SELECT value FROM memory_state WHERE key='context_cleared_at'), '')
                   ORDER BY started_at ASC""",
                (start.astimezone(timezone.utc).isoformat(),
                 end.astimezone(timezone.utc).isoformat()),
            ).fetchall()
        return [StoredTurn(UUID(row[0]), row[1] or "", row[2] or "", row[3]) for row in rows]

    def recent_conversations(self, limit: int = 30, offset: int = 0) -> list[ConversationRow]:
        limit = max(1, min(int(limit), 100))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, started_at, user_text, assistant_text FROM turns
                   WHERE status = 'completed' ORDER BY started_at DESC LIMIT ? OFFSET ?""",
                (limit, max(0, int(offset))),
            ).fetchall()
        return [ConversationRow(UUID(row[0]), row[1], row[2] or "", row[3] or "")
                for row in rows]

    def get_summary(self) -> str:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM memory_state WHERE key = 'rolling_summary'"
            ).fetchone()
        return row[0] if row else ""

    def save_summary(self, summary: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO memory_state(key, value, updated_at)
                   VALUES('rolling_summary', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_at=excluded.updated_at""",
                (summary, now),
            )
            connection.commit()

    def facts(self, limit: int = 50) -> list[tuple[str, str, float]]:
        with closing(self._connect()) as connection:
            return connection.execute(
                """SELECT key, value, confidence FROM memories
                   ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def upsert_facts(
        self, facts: list[tuple[str, str, float]], source_turn_id: UUID | None
    ) -> None:
        """Write several facts in one transaction.

        The single-fact call opens a fresh SQLite connection per fact, so a dozen
        facts meant a dozen connections, a dozen ``PRAGMA`` round trips and a
        dozen commits — the bulk of a sleep pass that had almost nothing to do.
        """
        if not facts:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = [(str(uuid4()), key, value, confidence, str(source_turn_id), now)
                for key, value, confidence in facts]
        with closing(self._connect()) as connection:
            connection.executemany(
                """INSERT INTO memories
                   (id, key, value, confidence, source_turn_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value,
                     confidence=excluded.confidence,
                     source_turn_id=excluded.source_turn_id,
                     updated_at=excluded.updated_at""",
                rows,
            )
            connection.commit()

    def upsert_fact(
        self, key: str, value: str, confidence: float, source_turn_id: UUID
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO memories
                   (id, key, value, confidence, source_turn_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value,
                     confidence=excluded.confidence,
                     source_turn_id=excluded.source_turn_id,
                     updated_at=excluded.updated_at""",
                (str(uuid4()), key, value, confidence, str(source_turn_id), now),
            )
            connection.commit()

    def fact_count(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) FROM memories").fetchone()
        return int(row[0]) if row else 0

    def oldest_fact_keys(self, limit: int) -> list[str]:
        """The least recently confirmed facts, for eviction when the table is full.

        A durable fact gets re-confirmed over time, which refreshes its timestamp,
        so the oldest rows are the ones nothing has needed to restate.
        """
        if limit <= 0:
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT key FROM memories ORDER BY updated_at ASC, key ASC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [row[0] for row in rows]

    def delete_facts(self, keys: list[str]) -> None:
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        with closing(self._connect()) as connection:
            connection.execute(
                f"DELETE FROM memories WHERE key IN ({placeholders})", keys
            )
            connection.commit()

    def record_consolidation(self, day: str, at: datetime | None = None) -> None:
        """Note that a day has been consolidated, so any interface can report it.

        Stored in ``memory_state`` because it describes the memory itself. The
        sleep status file is written by whichever process ran the pass; this is
        the copy every process can read without knowing anything about it.
        """
        now = (at or datetime.now(timezone.utc)).isoformat()
        with closing(self._connect()) as connection:
            for key, value in (("last_consolidation_day", day),
                               ("last_consolidation_at", now)):
                connection.execute(
                    """INSERT INTO memory_state(key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                       updated_at=excluded.updated_at""",
                    (key, value, now),
                )
            connection.commit()

    def state_value(self, key: str) -> str:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM memory_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else ""

    def clear_context(self) -> str:
        """Reset the context boundary, retaining history and durable facts."""
        stamp = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("INSERT INTO memory_state(key,value,updated_at) VALUES('context_cleared_at',?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (stamp, stamp))
            connection.execute("DELETE FROM memory_state WHERE key='rolling_summary'")
            connection.commit()
        return stamp
