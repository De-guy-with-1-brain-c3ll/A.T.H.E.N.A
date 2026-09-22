"""Sleep mode as a tool, so ATHENA can consolidate memory when asked.

Two tools, deliberately separate:

``sleep_mode`` starts a consolidation. It runs the pass **in the background** when
an interface is running one (voice, the dashboard), because a strong-model pass
over a full day takes longer than a spoken turn can wait — running it inline made
the tool call hit its own timeout and report a failure for work that had actually
finished. With no coordinator (the CLI, the text interface, Feishu) it runs inline
and returns the result.

``sleep_status`` reports. It never changes anything, so it is always safe, and it
reads the shared status record rather than this process's memory — so it answers
correctly even when another interface is the one consolidating.
"""
from __future__ import annotations

import os
from datetime import date

from athena.sleep import (
    SleepBusy,
    SleepStatus,
    SleepStatusStore,
    consolidate,
    last_consolidated_date,
    status_report,
)
from athena.tools.models import ToolDefinition, ToolResult


def _parse_day(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


class SleepModeTool:
    definition = ToolDefinition(
        name="sleep_mode",
        description=(
            "Consolidate a day of short-term conversation into long-term memory using the "
            "stronger DeepSeek model. Use when Benjamin says to go to sleep, to sleep on it, "
            "or asks ATHENA to remember today properly. It returns immediately and keeps "
            "working in the background; use sleep_status to report how it went."
        ),
        parameters={"type": "object", "properties": {
            "day": {"type": "string", "maxLength": 10,
                    "description": "Day to consolidate as YYYY-MM-DD. Omit for today."},
        }, "additionalProperties": False},
        # Inline mode runs the whole pass, so this is far longer than a normal tool.
        timeout_seconds=240,
    )

    def __init__(self, coordinator=None, status: SleepStatusStore | None = None,
                 runner=None):
        self.coordinator = coordinator
        self.store = status or SleepStatusStore()
        self.runner = runner

    def bind(self, services):
        # The voice coordinator owns the "consolidate in the background" path.
        if self.coordinator is None:
            self.coordinator = services.get("sleep_coordinator")
        if self.runner is None:
            self.runner = services.get("sleep_runner")

    async def execute(self, arguments):
        raw = str(arguments.get("day") or "").strip()
        day = None
        if raw:
            day = _parse_day(raw)
            if day is None:
                return ToolResult(False, f"{raw} is not a date like 2026-09-16.")

        background = getattr(self.coordinator, "start_sleep", None)
        if background is not None:
            return await background(day)

        # Running inline is a real wait, so refuse rather than queue behind a pass
        # that is already in flight: two passes over one day is wasted money. This
        # is checked before the credential, because "already running" is the more
        # useful answer when both are true.
        running = self.store.load()
        if running.is_live() and running.pid != os.getpid():
            return ToolResult(False, f"Already consolidating: {running.describe()}")

        try:
            if self.runner is not None:
                # SleepRunner.consolidate takes dry_run as keyword-only, so this
                # must not be a bare positional call through the same expression.
                report = await self.runner.consolidate(day)
            else:
                report = await consolidate(day)
        except SleepBusy as error:
            busy = SleepStatus.from_dict(error.record)
            return ToolResult(False, f"Already consolidating: {busy.describe()}")
        except Exception as error:
            return ToolResult(False, f"Sleep mode could not run: {error}")
        detail = report.__dict__
        if report.skipped:
            return ToolResult(True, f"Nothing to consolidate — {report.skipped}.", detail)
        spoken = (f"Consolidated {report.day}: read {report.turns} turn(s), wrote "
                  f"{report.facts_written} long-term fact(s) and forgot "
                  f"{report.facts_forgotten}, in {report.seconds:.0f} seconds.")
        if report.partial:
            spoken += (f" Part of that day could not be read"
                       f" ({len(report.failures)} of {report.chunks} parts), so run it"
                       f" again to finish the rest.")
        return ToolResult(True, spoken, detail)


class SleepStatusTool:
    definition = ToolDefinition(
        name="sleep_status",
        description=(
            "Report the state of memory consolidation: whether a pass is running right "
            "now, when the last one ran, what it produced, and which days still have "
            "conversation that has not been consolidated. Read-only. Use for 'did you "
            "consolidate', 'when did you last save your memory', 'what are you doing', "
            "or 'is your memory up to date'."
        ),
        parameters={"type": "object", "properties": {
            "verbose": {"type": "boolean", "default": False,
                        "description": "Include the waiting days and the model used."},
        }, "additionalProperties": False},
        timeout_seconds=10,
    )

    def __init__(self, status: SleepStatusStore | None = None):
        self.store = status or SleepStatusStore()

    async def execute(self, arguments):
        status = self.store.load()
        detail = status.to_dict()
        if status.phase == "idle":
            # A status file can be missing on a fresh install while the memory is
            # not: fall back to the database's own record before claiming never.
            last = last_consolidated_date()
            if last is None:
                return ToolResult(
                    True, "I have not consolidated my memory yet.", detail)
            return ToolResult(
                True,
                f"My last consolidation was {last.isoformat()}, before I started keeping"
                f" a status record.",
                {**detail, "last_day": last.isoformat()})
        if arguments.get("verbose"):
            return ToolResult(True, status_report(), detail)
        return ToolResult(True, status.describe(), detail)


def create_tools():
    return [SleepModeTool(), SleepStatusTool()]
