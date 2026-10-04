from athena.tools.models import ToolDefinition, ToolResult
from athena.voice_ipc import request_audio_route

class AudioRouteTool:
    definition = ToolDefinition(name="manage_audio_devices",
        description="Switch BOTH ATHENA microphone and speaker between the Pi configured devices and the connected computer's default devices, or get status. Computer requires its HTTPS dashboard audio connection and microphone permission first. Never claim a switch before this succeeds. Does not change Windows-wide audio settings.",
        parameters={"type": "object", "properties": {"target": {"enum": ["pi", "computer", "status"]}},
                    "required": ["target"], "additionalProperties": False}, timeout_seconds=15)
    async def execute(self, arguments):
        try:
            state = await request_audio_route(arguments["target"])
            return ToolResult(True, "My microphone and speaker are on your " + ("computer." if state["target"] == "computer" else "Pi."), state)
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            return ToolResult(False, str(error))
def create_tools(): return [AudioRouteTool()]
