"""Sleep mode: turn a day of conversation into long-term memory.

ATHENA has two kinds of memory.

**Short-term** is the raw turn log — everything said today, verbatim, in the
``turns`` table. It is what makes the current conversation coherent, and it is
deliberately unedited.

**Long-term** is the rolling summary and the durable facts in ``memories``. It is
small, curated, and what ATHENA actually carries forward.

The live concentrator updates long-term memory every few turns using the fast
model, so it stays cheap and immediate. Sleep mode is the deliberate pass: it
reads a whole day of short-term memory and hands it to a stronger model, which is
what turns a day of talking into memory worth keeping. Run it when the day is
over.

    athena-sleep                     # consolidate today
    athena-sleep --day 2026-09-15    # consolidate a specific day
    athena-sleep --dry-run           # report without writing
    athena-sleep --status            # what happened last, and what is left

The pass is a job with observable state rather than a black box: every step is
timestamped into a status record on disk, so ATHENA can answer "what are you
doing" and "when did you last do this" from any interface, including ones that do
not run the consolidation themselves.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time as clock
from uuid import uuid4
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from athena.memory.database import MemoryDatabase
from athena.memory.apply import apply_result, prune
from athena.memory.quality import MAX_FACTS
from athena.paths import data_directory, database_path
from athena.prompts import read_prompt


# The strong model does the consolidation. `flash` runs the live concentrator,
# so `pro` is the step up that sleep mode exists to use.
DEFAULT_SLEEP_MODEL = "deepseek-v4-pro"
FALLBACK_MODEL = "deepseek-v4-flash"
MAX_TURNS = 400
# Characters of conversation per request. The old code sent the whole day as one
# call: a long talking day meant a 20k-character prompt and 15 seconds of the
# strong model thinking about all of it at once. Reading it in overlapping chunks
# keeps each request short enough to answer quickly, and lets one slow request be
# retried without redoing the rest.
CHUNK_CHARS = 6000
CHUNK_OVERLAP_TURNS = 1
# What a single model request is allowed to take. Without this, one stalled
# connection held the whole pass open until something else failed.
MODEL_CALL_TIMEOUT_SECONDS = 90.0
# How long one interface will wait for another interface's pass before deciding it
# has gone stale. A real pass over a full day finishes well inside this.
STALE_AFTER_SECONDS = 900.0
# Status older than this is history rather than a current job.
STATUS_KEEP_SECONDS = 7 * 86400


def local_zone() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("ATHENA_TIMEZONE", "Asia/Shanghai"))
    except Exception:
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(local_zone())


def today() -> date:
    """The local day in progress.

    A consolidation runs at night and timestamps itself just after midnight, so
    the local wall clock would name tomorrow while the turns it actually read all
    belong to yesterday. Taking the timezone from the history closes that window,
    which a fixed local-midnight window does not.
    """
    return datetime.now(timezone.utc).astimezone(_zone_from_history()).date()


def _zone_from_history():
    """The timezone the turns table was written in, or the configured one."""
    fixed = local_zone()
    try:
        row = sqlite3.connect(database_path()).execute(
            "SELECT started_at FROM turns WHERE status = 'completed' "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
        stamp = datetime.fromisoformat(str(row[0])) if row and row[0] else None
    except (sqlite3.Error, TypeError, ValueError):
        stamp = None
    if stamp is None:
        return fixed
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    # The database is the only durable record of the offset that was in force at
    # the time, because SQLite stores the instant and drops the zone name.
    return stamp.tzinfo or fixed


def day_window(day: date) -> tuple[datetime, datetime]:
    """The local day as a UTC window, so 'today' means the user's today."""
    zone = local_zone()
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def chunk_turns(turns: list, limit: int = CHUNK_CHARS,
                overlap: int = CHUNK_OVERLAP_TURNS) -> list[list]:
    """Split a day into requests of a workable size.

    A turn is never split — half a turn reads as a different conversation — and
    chunks share their last ``overlap`` turns, so a decision that spans a boundary
    is still visible in both. A day under the limit is one chunk, which is the
    common case and stays exactly as cheap as before.
    """
    if not turns:
        return []
    chunks: list[list] = []
    current: list = []
    size = 0
    for turn in turns:
        cost = len(turn.user_text) + len(turn.assistant_text) + 40
        if current and size + cost > limit:
            chunks.append(current)
            current = current[-overlap:] if overlap else []
            size = sum(len(t.user_text) + len(t.assistant_text) + 40 for t in current)
        current.append(turn)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def merge_results(results: list[dict]) -> dict:
    """Fold per-chunk results into one, oldest chunk first.

    Later chunks win for a key they both describe, because they read the later
    conversation; a key any chunk asked to forget stays forgotten unless a later
    chunk deliberately restates it.

    Each chunk is asked to rewrite the *whole* summary, so the summaries are not
    appended — that produced "kept kept kept kept kept kept" for a summary that
    every chunk returned identically. Identical summaries collapse, and genuinely
    different ones are joined.
    """
    facts: dict[str, tuple[str, float]] = {}
    forget: list[str] = []
    summaries: list[str] = []
    for result in results:
        for key in result.get("forget_keys", []) or []:
            key = str(key).strip()[:80]
            if key:
                forget.append(key)
                facts.pop(key, None)
        for fact in result.get("facts", []) or []:
            key = str(fact.get("key", "")).strip()[:80]
            if not key:
                continue
            try:
                confidence = max(0.0, min(1.0, float(fact.get("confidence", 0))))
            except (TypeError, ValueError):
                continue
            facts[key] = (str(fact.get("value", "")).strip()[:500], confidence)
        part = " ".join(str(result.get("summary", "")).split())
        if part and part not in summaries:
            summaries.append(part)
    merged = {
        "summary": " ".join(summaries)[:2000],
        "facts": [{"key": key, "value": value, "confidence": confidence}
                  for key, (value, confidence) in facts.items()][:12],
        "forget_keys": sorted(set(forget)),
    }
    return merged


# ---- Status ------------------------------------------------------------------
#
# A pass is a job, and a job has to be checkable while it runs and afterwards.
# The record lives in the shared data directory rather than in one process's
# memory: the voice service starts the pass, but the dashboard, Feishu and the CLI
# all have to be able to answer for it. Nothing sensitive goes in — a day name, a
# count and a timestamp.


class SleepBusy(RuntimeError):
    """Another interface is already consolidating."""

    def __init__(self, record: dict | None = None) -> None:
        self.record = record or {}
        super().__init__("A consolidation is already running.")


class ProviderError(RuntimeError):
    """The model did not answer at all, as opposed to answering badly."""


@dataclass
class SleepStatus:
    phase: str = "idle"          # idle | starting | reading | thinking | writing | done | failed | skipped
    day: str = ""
    model: str = ""
    started_at: str = ""
    started_monotonic: float = 0.0
    heartbeat_at: str = ""
    pid: int = 0
    turns: int = 0
    requests: int = 0
    chunks: int = 0
    facts_written: int = 0
    facts_forgotten: int = 0
    offered_refused: int = 0
    summary_chars: int = 0
    seconds: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "SleepStatus":
        known = {key: raw[key] for key in cls().to_dict() if key in raw}
        return cls(**known)

    @property
    def finished(self) -> bool:
        return self.phase in {"done", "failed", "skipped"}

    def heartbeat_age(self) -> float:
        try:
            stamp = datetime.fromisoformat(self.heartbeat_at)
        except (TypeError, ValueError):
            return float("inf")
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())

    def is_live(self) -> bool:
        """Running right now, in this process or another one."""
        return not self.finished and self.heartbeat_age() < STALE_AFTER_SECONDS

    def describe(self) -> str:
        if self.phase == "idle":
            return "I've never consolidated memory."
        when = self._local(self.started_at)
        if self.phase == "failed":
            return f"Consolidation of {self.day} failed: {self.detail or 'no reason recorded'}."
        if not self.finished:
            did = self._progress_clause()
            return f"Consolidating {self.day} right now — {did}."
        if self.phase == "skipped":
            return f"I checked {self.day} and there was nothing to consolidate."
        facts = self.facts_written
        line = (f"Last consolidated {self.day} on {when} — read {self.turns} "
                f"turn{'s' if self.turns != 1 else ''} and wrote {facts} "
                f"fact{'s' if facts != 1 else ''}")
        if self.facts_forgotten:
            line += f", forgot {self.facts_forgotten}"
        if self.summary_chars:
            line += f", summary {self.summary_chars} characters"
        if self.detail:
            line += f", {self.detail}"
        return line + f", in {self.seconds:.1f} seconds."

    def _progress_clause(self) -> str:
        phase = {"starting": "starting", "reading": "reading the day",
                 "thinking": "reading it through",
                 "writing": "writing to memory"}.get(self.phase, self.phase)
        if self.phase == "thinking" and self.chunks > 1:
            phase = f"reading it through, part {min(self.requests + 1, self.chunks)} of {self.chunks}"
        elapsed = self.seconds
        if not elapsed and self.started_monotonic:
            elapsed = clock.monotonic() - self.started_monotonic
        if elapsed:
            return f"{phase}, {elapsed:.0f} seconds in"
        return phase

    def summary_line(self) -> str:
        """One line for the dashboard."""
        if self.phase == "idle":
            return "no consolidation recorded"
        if self.is_live():
            return f"{self.phase}: {self.day} ({self._progress_clause()})"
        if self.phase == "failed":
            return f"{self.phase}: {self.day} — {self.detail}"
        if self.phase == "skipped":
            return f"{self.phase}: {self.day} — nothing to consolidate"
        return (f"{self.phase}: {self.day} — {self.turns} turn(s), "
                f"{self.facts_written} fact(s), {self.seconds:.1f}s")

    @staticmethod
    def _local(value: str) -> str:
        try:
            stamp = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return "an unknown time"
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        stamp = stamp.astimezone(local_zone())
        today_date = today()
        clock_text = stamp.strftime("%I:%M %p").lstrip("0")
        if stamp.date() == today_date:
            return f"{clock_text} today"
        if stamp.date() == today_date - timedelta(days=1):
            return f"{clock_text} yesterday"
        return f"{clock_text} on {stamp:%A}"


class SleepStatusStore:
    """The status record several processes share. Never raises on I/O."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_directory() / "sleep-status.json"

    def load(self) -> SleepStatus:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return SleepStatus()
        if not isinstance(raw, dict):
            return SleepStatus()
        try:
            return SleepStatus.from_dict(raw)
        except TypeError:
            return SleepStatus()

    def _write(self, status: SleepStatus) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(status.to_dict(), ensure_ascii=False, indent=2)
                                 + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            # A status record is not worth failing a consolidation over.
            pass

    def begin(self, day: date, model: str, turns: int) -> SleepStatus:
        """Claim the job, refusing to start on top of a live one."""
        current = self.load()
        if current.is_live() and current.pid != os.getpid():
            raise SleepBusy(current.to_dict())
        status = SleepStatus(
            phase="starting", day=day.isoformat(), model=model, turns=turns,
            pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat(),
            started_monotonic=clock.monotonic(),
            heartbeat_at=datetime.now(timezone.utc).isoformat())
        self._write(status)
        return status

    def update(self, status: SleepStatus, **changes) -> SleepStatus:
        for key, value in changes.items():
            setattr(status, key, value)
        status.heartbeat_at = datetime.now(timezone.utc).isoformat()
        if status.started_monotonic:
            status.seconds = clock.monotonic() - status.started_monotonic
        self._write(status)
        return status

    def finish(self, status: SleepStatus, *, phase: str, **changes) -> SleepStatus:
        status = self.update(status, phase=phase, **changes)
        status.seconds = round(status.seconds, 1)
        self._write(status)
        return status

    def clear_if_older_than(self, seconds: int = STATUS_KEEP_SECONDS) -> None:
        status = self.load()
        if status.phase == "idle":
            return
        try:
            stamp = datetime.fromisoformat(status.started_at)
        except (TypeError, ValueError):
            return
        if (datetime.now(timezone.utc) - stamp).total_seconds() > seconds:
            self._write(SleepStatus())


def sleep_status() -> SleepStatus:
    """The current or most recent pass, as any interface can see it."""
    return SleepStatusStore().load()


def sleep_status_line() -> str:
    """What the dashboard asks for: one line."""
    return sleep_status().summary_line()


def last_consolidated_date() -> date | None:
    """The last day a pass completed, from the database.

    The status file answers for the run; the database answers for the memory, and
    it is written by whichever process actually consolidated. Reading it means a
    status file that was lost, never written, or written by another machine cannot
    make ATHENA claim it has never consolidated.
    """
    try:
        raw = MemoryDatabase(database_path()).state_value("last_consolidation_day")
    except Exception:
        return None
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def pending_days(now: datetime | None = None, *, limit: int = 14) -> list[str]:
    """Days since the last consolidation that have conversation but no pass yet."""
    moment = now or now_local()
    current = moment.date()
    last = last_consolidated_date()
    start = (last + timedelta(days=1)) if last else (current - timedelta(days=1))
    days: list[str] = []
    cursor = start
    examined = 0
    while cursor <= current and examined < limit + 7:
        examined += 1
        if cursor == current and now is None and moment.hour < 22:
            # Today is not unconsolidated yet, it is simply still happening. When
            # a caller supplies a time it is asking about a specific moment, so the
            # evening rule does not apply.
            cursor += timedelta(days=1)
            continue
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days[-limit:]


def status_report() -> str:
    """The whole picture, for a question rather than a glance."""
    status = sleep_status()
    lines = [status.describe()]
    if status.phase != "idle" and status.model and status.is_live():
        lines.append(f"  using : {status.model}")
    if status.phase not in {"idle", "starting"} and status.turns:
        lines.append(f"  read  : {status.turns} turn(s) in {status.chunks or 1} pass(es)")
    if status.phase == "done":
        lines.append(f"  refused: {status.offered_refused} fact(s) the quality rules would not keep")
    waiting = pending_days()
    if waiting:
        lines.append(f"  waiting on: {', '.join(waiting)}")
    else:
        lines.append("  waiting on: nothing — every day with conversation has been consolidated")
    return "\n".join(lines)


# ---- The pass ----------------------------------------------------------------


@dataclass
class SleepReport:
    day: str
    model: str
    turns: int = 0
    facts_written: int = 0
    facts_forgotten: int = 0
    summary_chars: int = 0
    seconds: float = 0.0
    skipped: str | None = None
    used_fallback: bool = False
    pruned: int = 0
    evicted: int = 0
    requests: int = 0
    chunks: int = 0
    offered_refused: int = 0
    detail: str = ""
    failures: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        """Some of the day was read, but not all of it.

        Silence here would be the dangerous outcome: a partial pass looks exactly
        like a clean one, and the unread turns are then never consolidated.
        """
        return bool(self.failures)

    def describe(self) -> str:
        if self.skipped:
            return f"Sleep mode: nothing to do — {self.skipped}."
        lines = [f"Sleep mode consolidated {self.day}."]
        if self.partial:
            lines.append(f"  WARNING         : {len(self.failures)} part(s) of the day were not read"
                         f" — {'; '.join(self.failures)}")
            lines.append("                    run it again for this day to finish the rest.")
        lines.extend([
            f"  short-term read : {self.turns} turn(s) in {self.requests} request(s)",
            f"  model           : {self.model}"
            + ("  (the strong model failed, so the fast one was used)" if self.used_fallback else ""),
            f"  long-term facts : {self.facts_written} written, {self.facts_forgotten} forgotten",
            f"  refused by rules: {self.offered_refused} fact(s) the model offered",
            f"  pruned          : {self.pruned} junk fact(s) removed",
            f"  evicted         : {self.evicted} over the {MAX_FACTS}-fact cap",
            f"  summary         : {self.summary_chars} characters",
            f"  took            : {self.seconds:.1f}s",
        ])
        return "\n".join(lines)


class SleepCycle:
    """One consolidation pass over a day of short-term memory."""

    def __init__(
        self,
        database: MemoryDatabase,
        api_key: str,
        model: str = DEFAULT_SLEEP_MODEL,
        fallback_model: str = FALLBACK_MODEL,
        prompt: str | None = None,
        status: SleepStatusStore | None = None,
    ) -> None:
        self.database = database
        self.model = model
        self.fallback_model = fallback_model
        self.prompt = prompt or read_prompt("memory")
        self.status = status or SleepStatusStore()
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            # A stalled provider must not hold the pass open. Short per-request
            # clocks plus one retry beat one long wait ending in nothing.
            timeout=MODEL_CALL_TIMEOUT_SECONDS,
            max_retries=1,
        )

    async def run(self, day: date | None = None, *, dry_run: bool = False) -> SleepReport:
        day = day or today()
        started = clock.perf_counter()
        report = SleepReport(day=day.isoformat(), model=self.model)

        start, end = day_window(day)
        status = self.status.begin(day, self.model, 0)
        try:
            return await self._run(day, started, report, status, dry_run=dry_run)
        except SleepBusy:
            raise
        except asyncio.CancelledError:
            self.status.finish(status, phase="idle", detail="cancelled")
            raise
        except Exception as error:
            # Nothing timestamps the failure otherwise, and a pass that died at
            # 2am leaving "phase: thinking" behind reads as permanently running.
            self.status.finish(status, phase="failed", detail=str(error)[:200])
            raise

    async def _run(self, day: date, started: float, report: SleepReport,
                   status: SleepStatus, *, dry_run: bool) -> SleepReport:
        turns = await asyncio.to_thread(self.database.turns_between, *day_window(day))
        if not turns:
            report.skipped = f"no conversation was recorded on {day.isoformat()}"
            report.seconds = clock.perf_counter() - started
            self.status.finish(status, phase="skipped", detail=report.skipped,
                               seconds=report.seconds)
            return report
        report.turns = min(len(turns), MAX_TURNS)
        turns = turns[-MAX_TURNS:]
        chunks = chunk_turns(turns)
        report.chunks = len(chunks)
        self.status.update(status, phase="reading", turns=report.turns, chunks=len(chunks))

        # These three reads are independent, and together they used to cost three
        # sequential connection setups before the model was even asked.
        existing_facts, summary = await asyncio.gather(
            asyncio.to_thread(self.database.facts, 50),
            asyncio.to_thread(self.database.get_summary),
        )

        results: list[dict] = []
        used_fallback = False
        for index, chunk in enumerate(chunks, start=1):
            self.status.update(status, phase="thinking", requests=index - 1)
            messages = self._messages(chunk, summary, existing_facts, day,
                                      part=(index, len(chunks)))
            try:
                result, fell_back = await self._ask(messages)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # One unreadable part must not discard the rest of the day, but it
                # must be visible: a partial pass has to be distinguishable from a
                # complete one or the unread turns are silently lost.
                report.failures.append(f"part {index} of {len(chunks)}: {error}")
                continue
            used_fallback = used_fallback or fell_back
            results.append(result)
            report.requests = len(results)
            self.status.update(status, requests=report.requests)

        report.used_fallback = used_fallback
        if used_fallback:
            report.model = self.fallback_model
        if not results:
            raise ValueError("; ".join(report.failures) or "no part of the day could be read")

        result = merge_results(results)

        if not dry_run:
            self.status.update(status, phase="writing")
            # Clear out anything the old rules let through before consolidating.
            report.pruned = await prune(self.database)

        applied = await apply_result(self.database, result, turns[-1].turn_id,
                                     dry_run=dry_run)
        report.facts_written = applied.written
        report.facts_forgotten = applied.forgotten
        report.summary_chars = applied.summary_chars
        report.evicted = applied.evicted
        report.offered_refused = applied.offered_refused
        report.seconds = clock.perf_counter() - started

        if report.partial:
            report.detail = f"{len(report.failures)} unread part(s) of the day"
        if not dry_run and not report.partial:
            # Recording the day is what makes "have you consolidated today" a fact
            # rather than an assumption, and it happens only after the writes land.
            # A partial pass deliberately does not claim the day: marking it done
            # would hide the turns that were never read, and nothing would ever go
            # back for them.
            await asyncio.to_thread(self.database.record_consolidation, report.day)
        self.status.finish(
            status, phase="done", turns=report.turns, requests=report.requests,
            chunks=report.chunks, facts_written=report.facts_written,
            facts_forgotten=report.facts_forgotten,
            offered_refused=report.offered_refused,
            summary_chars=report.summary_chars, seconds=round(report.seconds, 1),
            detail=report.detail)
        return report

    def _messages(self, chunk: list, summary: str, existing_facts: list,
                  day: date, *, part: tuple[int, int]) -> list[dict]:
        conversation = "\n\n".join(
            f"TURN {turn.turn_id}\nBENJAMIN: {turn.user_text}\nATHENA: {turn.assistant_text}"
            for turn in chunk
        )
        heading = "A FULL DAY OF CONVERSATION" if part[1] == 1 else "MORE OF THE SAME DAY"
        if part[1] > 1:
            heading += (f" (part {part[0]} of {part[1]}; earlier parts have already been"
                        " folded into the summary above)")
        return [
            {"role": "system", "content": self.prompt},
            {
                "role": "user",
                "content": (
                    f"EXISTING SUMMARY:\n{summary or '(none)'}\n\n"
                    "EXISTING FACTS:\n"
                    + ("\n".join(f"{key}: {value}" for key, value, _ in existing_facts) or "(none)")
                    + f"\n\n{heading} ({day.isoformat()}):\n{conversation}"
                ),
            },
        ]

    async def _ask(self, messages: list[dict]) -> tuple[dict, bool]:
        """Ask the strong model, falling back if it is unavailable.

        The fallback is for a *provider* failure: the request never got an answer,
        so a cheaper model is worth trying. A response that arrived and could not
        be parsed is a different problem, and retrying it on the fast model just
        spends a second request to reach the same answer — that is what made a
        known-bad part look like a success.
        """
        try:
            return await self._complete(self.model, messages), False
        except ProviderError as error:
            if self.model == self.fallback_model:
                raise
            print(f"Sleep mode: {self.model} failed ({error}); using {self.fallback_model}.",
                  file=sys.stderr)
            return await self._complete(self.fallback_model, messages), True

    async def _complete(self, model: str, messages: list[dict]) -> dict:
        attempt_messages = list(messages)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with asyncio.timeout(MODEL_CALL_TIMEOUT_SECONDS):
                    response = await self._client.chat.completions.create(
                        model=model,
                        messages=attempt_messages,
                        temperature=0.0,
                        max_tokens=1600,
                        response_format={"type": "json_object"},
                        extra_body={"thinking": {"type": "disabled"}},
                    )
            except (TimeoutError, asyncio.TimeoutError) as error:
                # A timeout, a dropped connection and an HTTP error are all the
                # provider not answering: the caller may try the fast model.
                raise ProviderError(f"{model} did not answer in "
                                    f"{MODEL_CALL_TIMEOUT_SECONDS:.0f}s") from error
            except Exception as error:
                raise ProviderError(f"{model} unavailable: {error}") from error
            raw = (response.choices[0].message.content or "").strip()
            try:
                if not raw:
                    raise ValueError("the model returned nothing")
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError) as error:
                # The provider answered; the answer was unusable. One more attempt
                # with a nudge, then this part of the day is reported as unread
                # rather than quietly counted as done.
                last_error = error
                if attempt == 0:
                    attempt_messages = attempt_messages + [
                        {"role": "user",
                         "content": "Return a smaller valid JSON object now."}]
        raise ValueError(f"the model returned invalid JSON twice: {last_error}")

    async def close(self) -> None:
        await self._client.close()


class SleepRunner:
    """Sleep mode bound to its long-lived state, so status survives the call.

    One instance per interface, created next to the tool registry. It is what the
    sleep tools and the coordinator share: the tools ask it to run a pass, the
    coordinator asks it to run a pass in the background, and the status tools read
    the record it writes.
    """

    def __init__(self, database: Path | None = None, api_key: str | None = None,
                 status: SleepStatusStore | None = None) -> None:
        self.database_path = database or database_path()
        self._api_key = api_key
        self.status = status or SleepStatusStore()

    @property
    def configured(self) -> bool:
        return bool(self._api_key or os.environ.get("DEEPSEEK_API_KEY", "").strip())

    def is_running(self) -> bool:
        """A pass is in flight, whether this process started it or not."""
        record = self.status.load()
        return record.is_live() and record.pid != os.getpid()

    async def consolidate(self, day: date | None = None, *,
                          dry_run: bool = False) -> SleepReport:
        key = self._api_key or os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured, so sleep mode cannot run.")
        store = MemoryDatabase(self.database_path)
        await asyncio.to_thread(store.initialize)
        cycle = SleepCycle(store, key, sleep_model(), status=self.status)
        try:
            return await cycle.run(day, dry_run=dry_run)
        finally:
            await cycle.close()


def sleep_model() -> str:
    return os.environ.get("ATHENA_SLEEP_MODEL", "").strip() or DEFAULT_SLEEP_MODEL


async def consolidate(day: date | None = None, *, dry_run: bool = False,
                      database: Path | None = None,
                      api_key: str | None = None) -> SleepReport:
    """One-shot helper for callers that do not want to hold the objects."""
    runner = SleepRunner(database=database, api_key=api_key)
    return await runner.consolidate(day, dry_run=dry_run)


def main() -> int:
    from athena.config import load_local_environment

    load_local_environment()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", help="the day to consolidate, as YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be remembered without writing it")
    parser.add_argument("--model", help=f"override the model (default {DEFAULT_SLEEP_MODEL})")
    parser.add_argument("--status", action="store_true",
                        help="report the last pass and any day still waiting, then stop")
    args = parser.parse_args()

    if args.status:
        print(status_report())
        return 0
    if args.model:
        os.environ["ATHENA_SLEEP_MODEL"] = args.model
    day = None
    if args.day:
        try:
            day = date.fromisoformat(args.day)
        except ValueError:
            print(f"'{args.day}' is not a date like 2026-09-16.", file=sys.stderr)
            return 2
    job_id = uuid4().hex[:8]
    try:
        report = asyncio.run(consolidate(day, dry_run=args.dry_run))
    except SleepBusy as error:
        record = SleepStatus.from_dict(error.record)
        print(f"Sleep mode is already running: {record.summary_line()}", file=sys.stderr)
        return 3
    except Exception as error:
        print(f"Sleep mode failed: {error}", file=sys.stderr)
        return 1
    print(f"[{job_id}] {report.describe()}")
    if args.dry_run and report.skipped is None:
        print("  (dry run: nothing was written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
