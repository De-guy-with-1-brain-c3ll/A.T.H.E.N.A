"""Opt-in delegation to the locally installed OpenClaw agent."""
from athena.openclaw import command, installation
from athena.config import load_local_environment
from athena.openclaw_tasks import task_manager
from athena.tools.models import PermissionLevel, ToolDefinition, ToolResult
import os
from uuid import uuid4
from athena.pc_bridge import pc_request


class OpenClawTaskTool:
    def __init__(self, remote=False):
        self.remote = remote
    definition = ToolDefinition(
        name="openclaw_task",
        description="Use OpenClaw as ATHENA's coding tool: delegate requested code changes, programs, tests, fixes and PC automation. Provide the full objective and constraints. Starts a background task and returns its ID immediately. Use openclaw_status to check progress or obtain its final report. Submission is not completion. May modify files and execute commands; do not retry ambiguous submissions.",
        parameters={"type": "object", "properties": {
            "objective": {"type": "string", "minLength": 1, "maxLength": 12000},
        }, "required": ["objective"], "additionalProperties": False},
        permission=PermissionLevel.CONFIRM,
        timeout_seconds=45,
    )

    async def execute(self, arguments):
        ident = uuid4().hex[:12]
        try:
            if self.remote:
                result = await pc_request('/openclaw', {**arguments, 'action': 'start', 'id': ident}, timeout=8)
                return ToolResult(result['success'], result['spoken_text'], result.get('data', {}))
            ident = task_manager().start(arguments['objective'], ident)
            return started(ident)
        except Exception:
            return ToolResult(False, f"OpenClaw submission is unconfirmed. Check task {ident} before retrying.",
                              {'task_id': ident, 'status': 'unconfirmed'})


def started(ident):
    return ToolResult(True, f'OpenClaw task {ident} queued. You can keep chatting and ask for its progress or final report.',
                      {'task_id': ident, 'status': 'queued', 'background_started': True})


class OpenClawStatusTool:
    definition = ToolDefinition(name='openclaw_status',
        description="Read actual OpenClaw coding/PC task state and recorded tool activity, or retrieve its final agent report. Use the task ID returned by openclaw_task; omit it to list recent tasks. This does not start, retry or modify work. Agent reports are not independently verified completion evidence.",
        parameters={'type': 'object', 'properties': {'action': {'enum': ['status', 'report']},
                    'id': {'type': 'string', 'pattern': '^[a-f0-9]{12}$'}},
                    'required': ['action'], 'additionalProperties': False}, timeout_seconds=45)

    def __init__(self, remote=False):
        self.remote = remote

    async def execute(self, arguments):
        try:
            if self.remote:
                result = await pc_request('/openclaw', arguments, timeout=8)
                return ToolResult(result['success'], result['spoken_text'], result.get('data', {}))
            rows = task_manager().rows(arguments.get('id'), report=arguments['action'] == 'report')
            if not rows:
                return ToolResult(False, 'No matching OpenClaw tasks are recorded.', {'tasks': [], 'status': 'none'})
            text = '; '.join(f"OpenClaw task {row['id']}: {row['state']}. " +
                ('Agent report (not independently verified): ' + row['report'] if row['report']
                 else row['progress']) for row in rows[:3])
            return ToolResult(True, text, {'tasks': rows, 'status': rows[0]['state'], 'untrusted_agent_report': True})
        except Exception:
            return ToolResult(False, 'OpenClaw task status could not be verified. The PC bridge may be offline; no task was restarted.')


class OpenClawBridge:
    definition = ToolDefinition(name='openclaw_bridge', description='Authenticated PC task bridge.',
        parameters={'type': 'object', 'properties': {
            'action': {'enum': ['start', 'status', 'report']},
            'id': {'type': 'string', 'pattern': '^[a-f0-9]{12}$'},
            'objective': {'type': 'string', 'minLength': 1, 'maxLength': 12000}},
            'additionalProperties': False,
            'allOf': [{'if': {'properties': {'action': {'enum': ['status', 'report']}}, 'required': ['action']},
                       'then': {'not': {'required': ['objective']}}, 'else': {'required': ['objective']}}]})

    async def execute(self, arguments):
        if arguments.get('action', 'start') == 'start':
            return started(task_manager().start(arguments['objective'], arguments.get('id')))
        return await OpenClawStatusTool().execute(arguments)


def create_tools():
    load_local_environment()
    try:
        command()
    except FileNotFoundError:
        # Keep configured capabilities visible even if a credential is missing.
        # Execution reports the configuration error instead of hiding the tool
        # and making status fall back to unrelated local receipts.
        if os.environ.get('ATHENA_PC_UPLOAD_URL'):
            return [OpenClawTaskTool(remote=True), OpenClawStatusTool(remote=True)]
        return []
    if not (installation() / "state" / "openclaw.json").is_file():
        return []
    return [OpenClawTaskTool(), OpenClawStatusTool()]
