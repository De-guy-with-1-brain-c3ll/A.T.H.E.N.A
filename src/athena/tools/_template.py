"""Copy this file, remove the leading underscore, and edit the marked fields."""

from athena.tools.models import PermissionLevel, ToolDefinition, ToolResult


class ExampleTool:
    definition = ToolDefinition(
        name="replace_with_unique_name",
        description="Describe exactly when the model should use this tool.",
        parameters={
            "type": "object",
            "properties": {
                "example_input": {
                    "type": "string",
                    "description": "Explain this input clearly.",
                }
            },
            "required": ["example_input"],
            "additionalProperties": False,
        },
        permission=PermissionLevel.SAFE,
        timeout_seconds=10.0,
        cancellable=True,
    )

    async def execute(self, arguments: dict) -> ToolResult:
        value = arguments["example_input"]
        # Put the actual tool work here. Avoid blocking the event loop; use
        # asyncio.to_thread(...) for slow synchronous libraries.
        return ToolResult(
            success=True,
            spoken_text=f"The tool completed with {value}.",
            data={"value": value},
        )


# Automatic discovery looks for this variable. No registry edit is needed.
TOOL = ExampleTool()
