"""Local alarm controls; all scheduling is handled without an LLM call."""
from __future__ import annotations

from datetime import datetime, timezone

from athena.alerts import describe_clock, describe_remaining
from athena.tools.models import ToolDefinition, ToolResult


UNAVAILABLE = "Alarms are unavailable until the local scheduler starts."


def _due(alarm: dict) -> datetime | None:
    try:
        due = datetime.fromisoformat(alarm["due_at"])
    except (KeyError, ValueError):
        return None
    return due if due.tzinfo else due.replace(tzinfo=timezone.utc)


def _seconds_left(due: datetime | None) -> int | None:
    if due is None:
        return None
    # Round, don't truncate: a ten minute timer measured a fraction of a second
    # later truncated to 599 and announced itself as "9 minutes".
    return int(round((due - datetime.now(timezone.utc)).total_seconds()))


def _describe(alarm: dict) -> dict:
    """Everything needed to confirm an alarm without waiting for it to ring."""
    due = _due(alarm)
    left = _seconds_left(due)
    return {
        "id": alarm.get("id"),
        "message": alarm.get("message"),
        "due_at": alarm.get("due_at"),
        "rings_at": describe_clock(due) if due else None,
        "seconds_remaining": left,
        "time_left": describe_remaining(left),
    }


class AlarmTool:
    definition = ToolDefinition(
        name="set_alarm",
        description=(
            "Set a local ATHENA alarm or timer. Use when like '10 minutes', '1 hour', "
            "or '7:30 PM'. The result confirms it was stored and reports how long is left."
        ),
        parameters={"type": "object", "properties": {
            "when": {"type": "string", "minLength": 2, "maxLength": 80},
            "message": {"type": "string", "minLength": 1, "maxLength": 300},
        }, "required": ["when", "message"], "additionalProperties": False},
    )

    def __init__(self, scheduler=None): self.scheduler = scheduler
    def bind(self, services): self.scheduler = services.get("alert_scheduler", self.scheduler)

    async def execute(self, arguments):
        if self.scheduler is None:
            return ToolResult(False, UNAVAILABLE)
        try:
            alarm = self.scheduler.add(arguments["when"], arguments["message"])
        except ValueError as error:
            return ToolResult(False, str(error))
        detail = _describe(alarm)
        # Confirm the instant that was actually stored and how far away it is.
        # Echoing the request back cannot be checked; a countdown and a clock
        # time can, so the timer is verifiable the moment it is set.
        label = "Timer" if (detail["seconds_remaining"] or 0) <= 3600 else "Alarm"
        spoken = (f"{label} set: {detail['time_left']} from now, "
                  f"ringing at {detail['rings_at']}.")
        detail["set"] = True
        return ToolResult(True, spoken, {"alarm": detail, **detail})


class ListAlarmsTool:
    definition = ToolDefinition(
        name="list_alarms",
        description=(
            "List active ATHENA alarms and timers with the time remaining on each. "
            "Use this to check whether a timer is set and how long is left."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    def __init__(self, scheduler=None): self.scheduler = scheduler
    def bind(self, services): self.scheduler = services.get("alert_scheduler", self.scheduler)
    async def execute(self, arguments):
        if self.scheduler is None:
            return ToolResult(False, UNAVAILABLE, {"alarms": []})
        rows = [row for row in self.scheduler.rows() if row.get("due_at")]
        if not rows:
            return ToolResult(True, "Nothing is set. No alarms, no timers.",
                              {"alarms": []})
        described = [_describe(row) for row in rows]
        summary = "; ".join(
            f"{row['message']} in {row['time_left']}" for row in described)
        return ToolResult(
            True,
            f"{len(described)} running: {summary}.",
            {"alarms": described},
        )


class CancelAlarmTool:
    definition = ToolDefinition(
        name="cancel_alarm",
        description="Cancel an ATHENA alarm or timer by its short ID.",
        parameters={"type": "object", "properties": {"alarm_id": {"type": "string", "minLength": 1, "maxLength": 20}}, "required": ["alarm_id"], "additionalProperties": False},
    )
    def __init__(self, scheduler=None): self.scheduler = scheduler
    def bind(self, services): self.scheduler = services.get("alert_scheduler", self.scheduler)
    async def execute(self, arguments):
        if self.scheduler is None:
            return ToolResult(False, UNAVAILABLE)
        wanted = arguments["alarm_id"].strip().casefold()
        before = next((row for row in self.scheduler.rows()
                       if str(row.get("id", "")).startswith(wanted)), None)
        if not self.scheduler.cancel(arguments["alarm_id"]):
            return ToolResult(False, "I could not find one matching alarm.")
        if before is None:
            return ToolResult(True, "Cancelled.")
        # Say what was cancelled and how long it had left, so a mistaken cancel
        # is obvious straight away rather than when nothing rings.
        detail = _describe(before)
        return ToolResult(
            True,
            f"Cancelled {detail['message']} — it had {detail['time_left']} left.",
            {"alarm": detail, **detail},
        )


def create_tools():
    return [AlarmTool(), ListAlarmsTool(), CancelAlarmTool()]
