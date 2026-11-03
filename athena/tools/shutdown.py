"""Graceful assistant exit. Never shut down the operating system."""
from athena.tools.models import ToolDefinition, ToolResult


class ShutdownTool:
    definition = ToolDefinition(
        name="shutdown_athena",
        description="Stop ATHENA and release microphone/audio resources, not Windows. Only works after a direct shutdown command from the user.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False})

    async def execute(self, arguments):
        return ToolResult(True, "Shutting down ATHENA. Goodbye.", {"shutdown_requested": True})


def create_tools():
    return [ShutdownTool()]
