"""Supervisor controls, available to every ATHENA interface."""
from athena.tools.models import ToolDefinition, ToolResult
from athena.workflows import ALLOWED


class AgentTaskTool:
    definition = ToolDefinition(
        name='agent_task',
        description='Spawn supervised background subagents for extensive research, coding or multi-step work. Return immediately. Check status/progress/evidence, steer or cancel by ID. Agents can delegate bounded children and verify reports. Shared token/time ceilings; no shell/download/upload/settings permissions. Use only for requested work, never for greetings or simple commands.',
        parameters={'type': 'object', 'properties': {
            'action': {'enum': ['spawn', 'status', 'report', 'steer', 'cancel']},
            'objective': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
            'id': {'type': 'string', 'minLength': 1, 'maxLength': 40},
            'message': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
            'tools': {'type': 'array', 'maxItems': 12, 'minItems': 1,
                      'items': {'type': 'string', 'enum': sorted(ALLOWED)}},
            'token_budget': {'type': 'integer', 'minimum': 4000, 'maximum': 100000},
            'time_limit_seconds': {'type': 'integer', 'minimum': 30, 'maximum': 600},
        }, 'required': ['action'], 'additionalProperties': False})

    def __init__(self, manager=None, parent=None):
        self.manager, self.parent = manager, parent

    def bind(self, services):
        self.manager = services.get('agent_supervisor', self.manager)

    async def execute(self, arguments):
        if self.manager is None:
            return ToolResult(False, 'Background agent supervisor is unavailable.')
        try:
            action = arguments['action']
            if action == 'spawn':
                ident = self.manager.spawn(arguments['objective'], arguments.get('tools'), self.parent,
                                           arguments.get('token_budget', 60000), arguments.get('time_limit_seconds', 300))
                return ToolResult(True, f'Agent {ident} queued. You can keep chatting and ask for its progress.',
                                  {'agent_id': ident, 'status': 'queued', 'background_started': True})
            ident = arguments.get('id')
            if self.parent and ident and ident not in {row['id'] for row in self.manager.rows(parent=self.parent)}:
                raise ValueError('An agent may supervise only its own children.')
            if action == 'cancel':
                ok = self.manager.cancel(arguments['id'])
                return ToolResult(ok, 'Cancellation requested for the agent and its children.' if ok else 'Agent not found.')
            if action == 'steer':
                self.manager.steer(arguments['id'], arguments['message'])
                return ToolResult(True, 'Guidance queued for its next model step.')
            rows = self.manager.rows(ident, self.parent if not ident else None)
            if not rows:
                return ToolResult(False, 'No matching agents recorded.', {'agents': []})
            text = '; '.join(f"{row['id']} ({row['objective'][:100]}): {row['state']}. " +
                             (row['report'][:1000] if action == 'report' else row['progress']) for row in rows[:5])
            compact = [{key: row[key] for key in ('id', 'parent', 'root', 'state', 'spent', 'budget')} |
                       {'objective': row['objective'][:200], 'progress': row['progress'][:300],
                        'report': row['report'][:1000] if action == 'report' else '',
                        'evidence': [{'id': e['id'], 'tool': e['tool'], 'success': e['success']}
                                     for e in row['evidence'][-10:]]} for row in rows[:5]]
            return ToolResult(True, text, {'agents': compact})
        except (KeyError, ValueError) as error:
            return ToolResult(False, str(error))


def create_tools():
    return [AgentTaskTool()]
