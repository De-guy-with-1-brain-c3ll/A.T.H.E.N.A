"""Turn a model memory result into stored long-term memory, in exactly one place.

The live concentrator and sleep mode produce the same JSON shape, so they have to
apply the same rules: the sensitive-data rule, the quality gate, and the cap that
keeps the table small enough to be useful. Keeping that in one function is what
stops the two paths drifting apart.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from athena.memory.database import MemoryDatabase
from athena.memory.quality import (
    MAX_FACTS,
    SENSITIVE_PATTERN,
    is_durable,
    rejection_reason,
)


@dataclass
class Applied:
    written: int = 0
    forgotten: int = 0
    rejected: list[str] = field(default_factory=list)
    summary_chars: int = 0
    evicted: int = 0
    # Facts the model offered and the quality gate refused. Reported so "nothing
    # was written" can be told apart from "nothing usable was offered".
    offered_refused: int = 0

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


def _clean(result: dict) -> tuple[str, list[str], list[tuple[str, str, float]], int]:
    """Pull the summary, the forget list and the acceptable facts out of a result."""
    summary = str(result.get("summary", "")).strip()[:2000]
    if summary and SENSITIVE_PATTERN.search(summary):
        summary = ""

    forget_keys = [
        str(key).strip()[:80]
        for key in result.get("forget_keys", [])[:40]
        if str(key).strip() and not SENSITIVE_PATTERN.search(str(key))
    ]

    facts: list[tuple[str, str, float]] = []
    refused = 0
    for fact in result.get("facts", [])[:12]:
        key = str(fact.get("key", "")).strip()[:80]
        value = str(fact.get("value", "")).strip()[:500]
        try:
            confidence = max(0.0, min(1.0, float(fact.get("confidence", 0))))
        except (TypeError, ValueError):
            refused += 1
            continue
        if confidence < 0.65:
            refused += 1
            continue
        if SENSITIVE_PATTERN.search(key) or SENSITIVE_PATTERN.search(value):
            refused += 1
            continue
        reason = rejection_reason(key, value)
        if reason:
            refused += 1
            continue
        facts.append((key, value, confidence))
    return summary, forget_keys, facts, refused


async def apply_result(database: MemoryDatabase, result: dict, source_turn_id: UUID | None,
                       *, dry_run: bool = False) -> Applied:
    """Filter a model result and store it, then keep the table within its cap."""
    applied = Applied()
    summary, forget_keys, facts, refused = _clean(result)
    applied.offered_refused = refused
    combined = sorted(set(forget_keys) | set(
        key for key, value, _ in await asyncio.to_thread(database.facts, 500)
        if rejection_reason(key, value) is not None))
    applied.rejected = [key for key in combined if key not in set(forget_keys)]

    applied.forgotten = len(combined)
    applied.written = len(facts)
    applied.summary_chars = len(summary)
    if dry_run:
        return applied

    if combined:
        await asyncio.to_thread(database.delete_facts, combined)
    if summary:
        await asyncio.to_thread(database.save_summary, summary)
    if facts:
        await asyncio.to_thread(database.upsert_facts, facts, source_turn_id)

    # Evict the least recently confirmed facts when the table is over its cap, so
    # it cannot grow without bound the way it did before.
    total = await asyncio.to_thread(database.fact_count)
    if total > MAX_FACTS:
        victims = await asyncio.to_thread(database.oldest_fact_keys, total - MAX_FACTS)
        await asyncio.to_thread(database.delete_facts, victims)
        applied.evicted = len(victims)
    return applied


async def prune(database: MemoryDatabase, *, dry_run: bool = False) -> int:
    """Remove every stored fact that fails the quality rules. Returns how many went."""
    existing = await asyncio.to_thread(database.facts, 1000)
    doomed = sorted({
        key for key, value, _ in existing if not is_durable(key, value)
    })
    if doomed and not dry_run:
        await asyncio.to_thread(database.delete_facts, doomed)
    return len(doomed)
