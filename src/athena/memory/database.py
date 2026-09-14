from __future__ import annotations

from dataclasses import dataclass
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
                (str(turn.turn_id), now, turn.user_text, turn.assistant_text),
            )
            connection.commit()

    def recent_turns(self, limit: int = 10) -> list[StoredTurn]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, user_text, assistant_text FROM turns
                   WHERE status = 'completed' ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        rows.reverse()
        return [StoredTurn(UUID(row[0]), row[1] or "", row[2] or "") for row in rows]

    def recent_conversations(self, limit: int = 30) -> list[ConversationRow]:
        limit = max(1, min(int(limit), 100))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, started_at, user_text, assistant_text FROM turns
                   WHERE status = 'completed' ORDER BY started_at DESC LIMIT ?""",
                (limit,),
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

    def delete_facts(self, keys: list[str]) -> None:
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        with closing(self._connect()) as connection:
            connection.execute(
                f"DELETE FROM memories WHERE key IN ({placeholders})", keys
            )
            connection.commit()
