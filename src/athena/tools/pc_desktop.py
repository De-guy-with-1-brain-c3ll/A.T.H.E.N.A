"""Open a visible terminal on the Windows desktop, locally or via the signed bridge."""
import os
from athena.pc_bridge import pc_request
from athena.pc_desktop import PCDesktop
from athena.tools.models import ToolDefinition, ToolResult


class PCDesktopTool:
    definition = ToolDefinition(name='pc_desktop',
        description="Open a visible PowerShell terminal on the user's Windows PC with its working directory set to Desktop. Works from the Pi through the authenticated PC bridge. This launches an empty interactive terminal; it cannot execute commands.",
        parameters={'type': 'object', 'properties': {'action': {'enum': ['open_terminal']}},
                    'required': ['action'], 'additionalProperties': False}, timeout_seconds=40)

    async def execute(self, arguments):
        try:
            result = (await PCDesktop().execute(arguments) if os.name == 'nt'
                      else await pc_request('/desktop', arguments))
            if not result.get('launched') or not result.get('pid'):
                return ToolResult(False, 'The PC did not confirm a terminal launch.', result)
            return ToolResult(True, 'Launched a PowerShell terminal on your PC, in your Desktop folder.', result)
        except Exception as error:
            return ToolResult(False, f'Could not open the PC terminal: {str(error)[:250]}')


def create_tools():
    return [PCDesktopTool()] if os.name == 'nt' or os.environ.get('ATHENA_PC_UPLOAD_URL') else []
