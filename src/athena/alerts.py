"""Persistent alarms and quiet proactive Teams due-date alerts."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import unicodedata
from typing import Awaitable, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from athena.paths import data_directory


Notify = Callable[[str], bool | Awaitable[bool]]
Watcher = Callable[[dict, dict], Awaitable[list[str]]]


# Spoken numbers matter because speech recognition usually returns words, not
# digits: "set an alarm for ten minutes" must work exactly like "for 10 minutes".
NUMBER_WORDS = {
    "zero": 0.0, "a": 1.0, "an": 1.0,
    "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
    "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
    "eleven": 11.0, "twelve": 12.0, "thirteen": 13.0, "fourteen": 14.0,
    "fifteen": 15.0, "sixteen": 16.0, "seventeen": 17.0, "eighteen": 18.0,
    "nineteen": 19.0, "twenty": 20.0, "thirty": 30.0, "forty": 40.0,
    "fourty": 40.0, "fifty": 50.0, "sixty": 60.0, "seventy": 70.0,
    "eighty": 80.0, "ninety": 90.0, "half": 0.5, "quarter": 0.25,
}


def parse_amount(text: str) -> float | None:
    """Read a quantity written as digits, words, or a mixture.

    Understands "10", "ten", "twenty five", "twenty-five", "half",
    "an" (as in "an hour"), and "one and a half".
    """
    cleaned = unicodedata.normalize("NFKC", str(text)).casefold()
    cleaned = cleaned.replace("-", " ").replace(",", " ")
    cleaned = re.sub(r"\b(?:a|an)\b", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        # "an hour" / "a minute" leaves nothing behind but still means one.
        return 1.0
    total = 0.0
    found = False
    for part in re.split(r"\band\b", cleaned):
        part = part.strip()
        if not part:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", part):
            total += float(part)
            found = True
            continue
        value = 0.0
        for token in part.split():
            if token not in NUMBER_WORDS:
                return None
            value += NUMBER_WORDS[token]
        total += value
        found = True
    return total if found else None


def describe_due(due: datetime, now: datetime | None = None) -> str:
    """Human wording for a stored alarm, in the configured local timezone.

    Confirming the resolved time (rather than echoing what was heard) is what
    makes an alarm verifiable: "Alarm set for 9:00 PM today" cannot be said
    unless that exact instant is really stored.
    """
    now = now or datetime.now(timezone.utc)
    try:
        zone = ZoneInfo(os.environ.get("ATHENA_TIMEZONE", "Asia/Shanghai"))
    except Exception:
        zone = timezone.utc
    local_due = due.astimezone(zone)
    local_now = now.astimezone(zone)
    delta = (due - now).total_seconds()
    if 0 < delta <= 3600:
        if delta < 60:
            seconds = max(1, int(round(delta)))
            return f"in {seconds} second" + ("" if seconds == 1 else "s")
        minutes = max(1, int(round(delta / 60)))
        return f"in {minutes} minute" + ("" if minutes == 1 else "s")
    clock = local_due.strftime("%I:%M %p").lstrip("0")
    days = (local_due.date() - local_now.date()).days
    if days == 0:
        return f"at {clock} today"
    if days == 1:
        return f"at {clock} tomorrow"
    return f"at {clock} on {local_due:%A}"


class _FileLock:
    """Cross-process lock so several ATHENA interfaces can share one alert file.

    The voice service, the dashboard and the Feishu connector each run their own
    scheduler against the same ``alerts.json``. Without a lock, whichever process
    saved last overwrote the others: an alarm set by voice was silently erased by
    the dashboard's next periodic save, and an alarm set from the dashboard never
    fired in the voice process at all.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        if os.name == "nt":
            import msvcrt
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            self._handle.close()
            self._handle = None


def _local_zone():
    try:
        return ZoneInfo(os.environ.get("ATHENA_TIMEZONE", "Asia/Shanghai"))
    except Exception:
        return timezone.utc


def describe_remaining(seconds: int | None) -> str:
    """How long is left, in the words a person would actually use.

    A timer has to be checkable the moment it is set, not only when it rings, so
    every alarm answer carries a countdown.
    """
    if seconds is None:
        return "an unknown time"
    if seconds <= 0:
        return "no time at all"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        text = f"{days} day" + ("" if days == 1 else "s")
        return f"{text} {hours} hour" + ("" if hours == 1 else "s") if hours else text
    if hours:
        text = f"{hours} hour" + ("" if hours == 1 else "s")
        return f"{text} {minutes} minute" + ("" if minutes == 1 else "s") if minutes else text
    if minutes:
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return f"{max(1, secs)} second" + ("" if secs == 1 else "s")


def describe_clock(due: datetime, now: datetime | None = None) -> str:
    """The wall-clock time a due moment lands on, so it can be checked at a glance."""
    now = now or datetime.now(timezone.utc)
    zone = _local_zone()
    local_due = due.astimezone(zone)
    local_now = now.astimezone(zone)
    clock = local_due.strftime("%I:%M %p").lstrip("0")
    days = (local_due.date() - local_now.date()).days
    if days == 0:
        return f"{clock} today"
    if days == 1:
        return f"{clock} tomorrow"
    return f"{clock} on {local_due:%A}"


class AlertScheduler:
    """Runs locally; it never calls DeepSeek and therefore costs no LLM tokens."""

    def __init__(self, notify: Notify, teams_graph=None, path: Path | None = None,
                 watchers: dict[str, Watcher] | None = None) -> None:
        self.notify = notify
        self.teams_graph = teams_graph
        self.path = path or data_directory() / "alerts.json"
        self.alarms: dict[str, dict] = {}
        self.teams_notified: dict[str, str] = {}
        # Watches are recurring checks that let ATHENA speak up unasked, for
        # example a new Teams message or the morning forecast.
        self.watches: dict[str, dict] = {}
        # Briefs are prepared summaries waiting to be offered. A daily watcher
        # builds one and stays silent; ATHENA offers it the next time Benjamin is
        # actually there to hear it.
        self.briefs: dict[str, dict] = {}
        self.watchers: dict[str, Watcher] = dict(watchers or {})
        # Shared clients a watcher may use; filled in by the interface wiring.
        self.watch_services: dict = {"teams_graph": teams_graph}
        self._task: asyncio.Task | None = None
        self._teams_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._lock = _FileLock(self.path.with_suffix(".lock"))
        self._stamp: tuple[int, int] | None = None
        self._load()

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
            self.alarms = {str(k): v for k, v in raw.get("alarms", {}).items() if isinstance(v, dict)}
            self.teams_notified = {str(k): str(v) for k, v in raw.get("teams_notified", {}).items()}
            self.watches = {str(k): v for k, v in raw.get("watches", {}).items() if isinstance(v, dict)}
            self.briefs = {str(k): v for k, v in raw.get("briefs", {}).items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError):
            self.alarms, self.teams_notified, self.watches, self.briefs = {}, {}, {}, {}
        self._stamp = self._file_stamp()

    def _refresh(self) -> bool:
        """Pick up another interface's changes. Returns True when reloaded."""
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return False
        self._load()
        return True

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"alarms": self.alarms, "teams_notified": self.teams_notified,
                                         "watches": self.watches, "briefs": self.briefs},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        self._stamp = self._file_stamp()

    def reload(self) -> None:
        """Public refresh, for callers that want the current shared state."""
        with self._lock:
            self._refresh()

    @staticmethod
    def parse_when(value: str, now: datetime | None = None) -> datetime:
        """Turn a spoken or typed time into an absolute UTC instant.

        Accepts relative offsets ("10 minutes", "in ten minutes", "half an hour"),
        clock times ("9 pm", "9p.m.", "7:30 pm", "at 7:30", "21:00"), and ISO
        timestamps. Punctuation and word numbers are both tolerated so speech
        recognition output and typed input behave the same.
        """
        text = unicodedata.normalize("NFKC", str(value)).casefold()
        # Trailing sentence punctuation is normal in typed input and speech
        # transcripts: "set an alarm for ten seconds." must parse like the same
        # words without the full stop.
        text = text.replace(",", " ").strip(" \t\r\n.?!;:")
        text = " ".join(text.split())
        now = now or datetime.now(timezone.utc)

        relative = re.fullmatch(r"(?:in\s+)?(.+?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)",
                                text)
        if relative:
            amount = parse_amount(relative.group(1))
            if amount is not None:
                unit = relative.group(2)
                seconds = amount * (86400 if unit.startswith("day")
                                    else 3600 if unit.startswith(("hour", "hr"))
                                    else 60 if unit.startswith(("minute", "min"))
                                    else 1)
                return now + timedelta(seconds=seconds)

        named = {"noon": (12, 0), "midday": (12, 0), "midnight": (0, 0)}
        # Words that move the day, and words that name a part of a day. Both are
        # ordinary in speech and used to fail outright, which pushed the request
        # to the model even though the time is unambiguous.
        day_offset = 0
        part_of_day = None
        day_words = re.search(
            r"\b(tomorrow\s+morning|tomorrow\s+afternoon|tomorrow\s+evening|tomorrow\s+night"
            r"|this\s+morning|this\s+afternoon|this\s+evening|tomorrow|today|tonight)\b",
            text)
        if day_words:
            phrase = re.sub(r"\s+", " ", day_words.group(1))
            if phrase.startswith("tomorrow"):
                day_offset = 1
            part_of_day = (9, 0) if "morning" in phrase else \
                          (15, 0) if "afternoon" in phrase else \
                          (20, 0) if phrase in {"tonight", "this evening"} or "evening" in phrase else \
                          (22, 0) if "night" in phrase else None
            text = " ".join(text.replace(day_words.group(1), " ").split())
            text = text.strip(" \t\r\n.?!;:")

        clock = re.fullmatch(r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\s*\.?\s*m\.?|p\s*\.?\s*m\.?)?",
                             text)
        resolved = None
        if text in named:
            resolved = named[text]
        elif clock:
            hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
            meridiem = re.sub(r"[^apm]", "", clock.group(3) or "")
            if minute > 59 or hour > 23 or (meridiem and not 1 <= hour <= 12):
                raise ValueError("That is not a valid time of day.")
            if meridiem:
                hour = hour % 12 + (12 if meridiem == "pm" else 0)
            resolved = (hour, minute)
        if resolved is None:
            # "tomorrow morning" gives a day and a part of a day but no clock.
            resolved = part_of_day
        if resolved is not None:
            hour, minute = resolved
            tz_name = os.environ.get("ATHENA_TIMEZONE", "Asia/Shanghai")
            try:
                zone = ZoneInfo(tz_name)
            except Exception:
                zone = timezone.utc
            local_now = now.astimezone(zone)
            target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            target += timedelta(days=day_offset)
            if day_offset == 0 and target <= local_now:
                target += timedelta(days=1)
            return target.astimezone(timezone.utc)

        try:
            parsed = datetime.fromisoformat(text.replace("z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
        except ValueError:
            raise ValueError("Use a time like '10 minutes', 'half an hour', or '7:30 PM'.") from None

    def add(self, when: str, message: str) -> dict:
        # Parse before locking: a bad time must not hold the file.
        due = self.parse_when(when)
        alarm = {"id": uuid4().hex[:8], "due_at": due.isoformat(),
                 "message": message.strip()[:300] or "Alarm.", "created_at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self._refresh()
            self.alarms[alarm["id"]] = alarm
            self._save()
        return alarm

    def cancel(self, alarm_id: str) -> bool:
        wanted = alarm_id.strip().casefold()
        with self._lock:
            self._refresh()
            matches = [key for key in self.alarms if key.startswith(wanted)]
            if len(matches) != 1:
                return False
            del self.alarms[matches[0]]
            self._save()
            return True

    def rows(self) -> list[dict]:
        """Current alarms, including ones another interface just created."""
        with self._lock:
            self._refresh()
            return sorted(self.alarms.values(), key=lambda row: row.get("due_at", ""))

    def add_watch(self, kind: str, params: dict, label: str,
                  interval_seconds: float = 300.0) -> dict:
        """Register a recurring check that can speak up on its own."""
        if kind not in self.watchers:
            raise ValueError("That kind of alert is not available yet.")
        watch = {
            "id": uuid4().hex[:8],
            "kind": kind,
            "label": label.strip()[:200] or kind,
            "params": params,
            "interval_seconds": max(60.0, float(interval_seconds)),
            "state": {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._refresh()
            self.watches[watch["id"]] = watch
            self._save()
        return watch

    def ensure_watch(self, kind: str, params: dict, label: str,
                     interval_seconds: float = 300.0) -> dict | None:
        """Register a recurring check only if the same one is not already there."""
        with self._lock:
            self._refresh()
            for watch in self.watches.values():
                if watch.get("kind") == kind and (watch.get("params") or {}) == params:
                    return None
        return self.add_watch(kind, params, label, interval_seconds)

    def cancel_watch(self, watch_id: str) -> bool:
        wanted = watch_id.strip().casefold()
        with self._lock:
            self._refresh()
            matches = [key for key in self.watches if key.startswith(wanted)]
            if len(matches) != 1:
                return False
            del self.watches[matches[0]]
            self._save()
            return True

    def save_brief(self, key: str, label: str, text: str) -> None:
        """Hold a prepared summary until Benjamin is around to be offered it."""
        with self._lock:
            self._refresh()
            self.briefs[key] = {
                "label": label.strip()[:120] or key,
                "text": text.strip()[:8000],
                "ready_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save()

    def take_brief(self, key: str) -> dict | None:
        with self._lock:
            self._refresh()
            brief = self.briefs.pop(key, None)
            if brief is not None:
                self._save()
            return brief

    def brief_rows(self) -> list[dict]:
        with self._lock:
            self._refresh()
            return [{"key": key, **value} for key, value in self.briefs.items()]

    def watch_rows(self) -> list[dict]:
        with self._lock:
            self._refresh()
            return sorted(self.watches.values(), key=lambda row: row.get("created_at", ""))

    async def start(self) -> None:
        if getattr(self, 'agents', None):
            await self.agents.start()
        if getattr(self, "workflows", None):
            await self.workflows.start()
        if self._task is None:
            self._task = asyncio.create_task(self._alarm_loop())
        if self.teams_graph is not None and self._teams_task is None:
            self._teams_task = asyncio.create_task(self._teams_loop())
        if self.watchers and self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch_loop())

    async def close(self) -> None:
        if getattr(self, 'agents', None):
            await self.agents.close()
        if getattr(self, "workflows", None):
            await self.workflows.close()
        tasks = [task for task in (self._task, self._teams_task, self._watch_task)
                 if task is not None]
        self._task = self._teams_task = self._watch_task = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_loop(self) -> None:
        while True:
            for watch_id, watch in self._claim_due_watches():
                watcher = self.watchers.get(str(watch.get("kind", "")))
                if watcher is None:
                    continue
                try:
                    messages = await watcher(watch, self.watch_services)
                except Exception:
                    # A failing watch stays quiet so it cannot spam the user;
                    # asking ATHENA about it directly still reports the error.
                    messages = []
                self._store_watch_state(watch_id, watch.get("state") or {})
                for message in messages:
                    await self._announce(message)
            await asyncio.sleep(5)

    def _claim_due_watches(self) -> list[tuple[str, dict]]:
        """Reserve the watches that are due, so only one interface runs them."""
        now = datetime.now(timezone.utc)
        claimed: list[tuple[str, dict]] = []
        with self._lock:
            self._refresh()
            for watch_id, watch in self.watches.items():
                try:
                    due = datetime.fromisoformat(
                        str(watch.get("next_check") or watch.get("created_at")))
                except (ValueError, TypeError):
                    due = now
                if due > now:
                    continue
                watch["next_check"] = self.next_check_for(watch, now).isoformat()
                claimed.append((watch_id, copy.deepcopy(watch)))
            if claimed:
                self._save()
        return claimed

    @staticmethod
    def next_check_for(watch: dict, now: datetime) -> datetime:
        """When a watch should next run.

        A watch with an ``at`` time runs once a day at that local time, which is
        what a daily briefing needs; otherwise it runs on its interval.
        """
        wanted = str((watch.get("params") or {}).get("at") or "").strip()
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", wanted)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                zone = _local_zone()
                local_now = now.astimezone(zone)
                target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= local_now:
                    target += timedelta(days=1)
                return target.astimezone(timezone.utc)
        return now + timedelta(seconds=float(watch.get("interval_seconds", 300)))

    def _store_watch_state(self, watch_id: str, state: dict) -> None:
        with self._lock:
            self._refresh()
            current = self.watches.get(watch_id)
            if current is None:
                return  # cancelled while it ran
            current["state"] = state
            self._save()

    def _merge_teams_notified(self, updates: dict[str, str]) -> None:
        if not updates:
            return
        with self._lock:
            self._refresh()
            self.teams_notified.update(updates)
            self._save()

    async def _announce(self, text: str) -> None:
        result = self.notify(text)
        if asyncio.iscoroutine(result):
            await result

    def claim_due_alarms(self, now: datetime | None = None) -> list[dict]:
        """Take ownership of the alarms that are due.

        Removal happens under the lock, so when several interfaces are running
        exactly one of them announces each alarm instead of all of them at once.
        """
        now = now or datetime.now(timezone.utc)
        claimed: list[dict] = []
        with self._lock:
            self._refresh()
            for key, alarm in list(self.alarms.items()):
                try:
                    due = datetime.fromisoformat(alarm["due_at"]).astimezone(timezone.utc)
                except (KeyError, ValueError, TypeError):
                    due = now
                if due <= now:
                    claimed.append(self.alarms.pop(key))
            if claimed:
                self._save()
        return claimed

    async def _alarm_loop(self) -> None:
        while True:
            for alarm in self.claim_due_alarms():
                await self._announce(f"Alarm: {alarm.get('message', 'It is time.')}")
            await asyncio.sleep(0.5)

    async def _teams_loop(self) -> None:
        interval = max(5.0, float(os.environ.get("ATHENA_TEAMS_POLL_MINUTES", "15")) * 60)
        threshold = max(1.0, float(os.environ.get("ATHENA_TEAMS_ALERT_HOURS", "24")) * 3600)
        while True:
            try:
                with self._lock:
                    self._refresh()
                assignments = await self.teams_graph.assignments(50)
                now = datetime.now(timezone.utc)
                updates: dict[str, str] = {}
                for item in assignments:
                    due_raw = item.get("dueDateTime")
                    if not due_raw or not item.get("id"):
                        continue
                    try:
                        due = datetime.fromisoformat(str(due_raw).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    delta = (due - now).total_seconds()
                    if delta < -86400 or delta > threshold:
                        continue
                    key = str(item["id"])
                    stamp = str(due_raw)
                    if self.teams_notified.get(key) == stamp or updates.get(key) == stamp:
                        continue
                    title = item.get("displayName") or "Teams assignment"
                    local_due = due.astimezone()
                    hour = local_due.strftime("%I").lstrip("0") or "0"
                    when = "overdue" if delta < 0 else f"due {local_due.strftime('%A')} at {hour}:{local_due:%M} {local_due:%p}"
                    await self._announce(f"Teams alert: {title} is {when}.")
                    updates[key] = stamp
                self._merge_teams_notified(updates)
            except Exception:
                # Authentication and network failures stay silent; asking ATHENA
                # explicitly will still return the useful error.
                pass
            await asyncio.sleep(interval)
