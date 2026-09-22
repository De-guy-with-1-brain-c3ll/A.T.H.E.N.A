"""Proactive alert controls: ATHENA speaks up when something changes.

Watches are recurring local checks (new Teams messages, weather). They never call
the language model, so an alert costs no tokens.
"""
from __future__ import annotations

from athena.tools.models import ToolDefinition, ToolResult


UNAVAILABLE = "Alerts are unavailable until the local scheduler starts."
DEFAULT_MORNING = "07:30"


class WatchTeamsTool:
    definition = ToolDefinition(
        name="watch_teams_channel",
        description=("Start a background alert that tells the user about new messages in a Microsoft "
                     "Teams channel. Read-only. Use when the user asks to be told about new Teams "
                     "messages or channel activity."),
        parameters={"type": "object", "properties": {
            "team": {"type": "string", "minLength": 1, "maxLength": 120},
            "channel": {"type": "string", "minLength": 1, "maxLength": 120},
            "minutes": {"type": "integer", "minimum": 1, "maximum": 120, "default": 5,
                        "description": "How often to check, in minutes."},
        }, "required": ["team", "channel"], "additionalProperties": False},
        timeout_seconds=25,
    )

    def __init__(self, scheduler=None): self.scheduler = scheduler
    def bind(self, services): self.scheduler = services.get("alert_scheduler", self.scheduler)

    async def execute(self, arguments):
        if self.scheduler is None:
            return ToolResult(False, UNAVAILABLE)
        team, channel = arguments["team"].strip(), arguments["channel"].strip()
        minutes = max(1, int(arguments.get("minutes", 5)))
        try:
            watch = self.scheduler.add_watch(
                "teams_messages", {"team": team, "channel": channel},
                f"new messages in {team} / {channel}", interval_seconds=minutes * 60)
        except ValueError as error:
            return ToolResult(False, str(error))
        return ToolResult(True, f"I will tell you about new messages in {channel}.",
                          {"watch": watch})


class WatchWeatherTool:
    definition = ToolDefinition(
        name="watch_weather",
        description=("Start a background weather alert. Use for 'tell me the weather every morning at "
                     "seven' or 'warn me if it is going to rain'. Read-only."),
        parameters={"type": "object", "properties": {
            "location": {"type": "string", "minLength": 2, "maxLength": 150},
            "at": {"type": "string", "minLength": 4, "maxLength": 5,
                   "pattern": "^[0-2]?[0-9]:[0-5][0-9]$",
                   "description": "Local 24-hour time for a daily briefing, for example 07:30."},
            "alert_if_rain_over": {"type": "integer", "minimum": 0, "maximum": 100,
                                   "description": "Speak up when the chance of precipitation reaches this percentage."},
        }, "required": ["location"], "additionalProperties": False},
        timeout_seconds=25,
    )

    def __init__(self, scheduler=None): self.scheduler = scheduler
    def bind(self, services): self.scheduler = services.get("alert_scheduler", self.scheduler)

    async def execute(self, arguments):
        if self.scheduler is None:
            return ToolResult(False, UNAVAILABLE)
        location = arguments["location"].strip()
        at = str(arguments.get("at") or "").strip()
        threshold = arguments.get("alert_if_rain_over")
        if not at and threshold is None:
            at = DEFAULT_MORNING
        params = {"location": location}
        if at:
            params["at"] = at
        if threshold is not None:
            params["alert_if_rain_over"] = threshold
        described = f"at {at} every day" if at else f"when rain reaches {threshold} percent"
        try:
            watch = self.scheduler.add_watch(
                "weather", params, f"weather for {location} {described}", interval_seconds=300)
        except ValueError as error:
            return ToolResult(False, str(error))
        return ToolResult(True, f"I will report the weather for {location} {described}.",
                          {"watch": watch})


class ListWatchesTool:
    definition = ToolDefinition(
        name="list_watches", description="List active background alerts (Teams messages, weather).",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    def __init__(self, scheduler=None): self.scheduler = scheduler
    def bind(self, services): self.scheduler = services.get("alert_scheduler", self.scheduler)
    async def execute(self, arguments):
        if self.scheduler is None:
            return ToolResult(False, UNAVAILABLE, {"watches": []})
        rows = self.scheduler.watch_rows()
        if not rows:
            return ToolResult(True, "There are no background alerts.", {"watches": []})
        described = [{"id": row.get("id"), "label": row.get("label"), "kind": row.get("kind")}
                     for row in rows]
        summary = "; ".join(f"{row['label']} ({row['id']})" for row in described)
        return ToolResult(True, f"You have {len(rows)} background alert(s): {summary}.",
                          {"watches": described})


class CancelWatchTool:
    definition = ToolDefinition(
        name="cancel_watch", description="Cancel a background alert by its short ID.",
        parameters={"type": "object", "properties": {
            "watch_id": {"type": "string", "minLength": 1, "maxLength": 20},
        }, "required": ["watch_id"], "additionalProperties": False},
    )
    def __init__(self, scheduler=None): self.scheduler = scheduler
    def bind(self, services): self.scheduler = services.get("alert_scheduler", self.scheduler)
    async def execute(self, arguments):
        if self.scheduler is None:
            return ToolResult(False, UNAVAILABLE)
        if self.scheduler.cancel_watch(arguments["watch_id"]):
            return ToolResult(True, "That alert is cancelled.")
        return ToolResult(False, "I could not find one matching alert.")


def create_tools():
    return [WatchTeamsTool(), WatchWeatherTool(), ListWatchesTool(), CancelWatchTool()]
