from __future__ import annotations

from datetime import datetime, timedelta, timezone as fixed_timezone
from zoneinfo import ZoneInfo

from athena.tools.models import ToolDefinition, ToolResult


class ClockTool:
    definition = ToolDefinition(
        name="get_local_time",
        description="Get the current date and time in an IANA timezone.",
        parameters={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone such as Asia/Shanghai",
                }
            },
            "required": ["timezone"],
            "additionalProperties": False,
        },
    )

    async def execute(self, arguments: dict) -> ToolResult:
        timezone = str(arguments["timezone"])
        try:
            if timezone.upper() == "UTC":
                zone = fixed_timezone.utc
            elif timezone == "Asia/Shanghai":
                # Reliable fallback on minimal Windows/Pi installations that
                # do not yet have the optional IANA tzdata package installed.
                zone = fixed_timezone(timedelta(hours=8), "Asia/Shanghai")
            else:
                zone = ZoneInfo(timezone)
            now = datetime.now(zone)
        except Exception:
            return ToolResult(False, f"I could not find the timezone {timezone}.")
        spoken = now.strftime("It is %I:%M %p on %A, %B %d.").replace(" 0", " ")
        return ToolResult(True, spoken, {"iso": now.isoformat(), "timezone": timezone})


TOOL = ClockTool()
