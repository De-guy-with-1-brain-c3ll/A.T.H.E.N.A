"""Bounded execution receipts, shared by worker forks and optionally interfaces."""
import re
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from athena.tools.models import ToolDefinition, ToolResult


class ToolStatus:
    def __init__(self, path=None):
        self.path = str(path) if path else ':memory:'
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=2)
        self.db.execute('CREATE TABLE IF NOT EXISTS operations '
                        '(id TEXT PRIMARY KEY, tool TEXT, label TEXT, state TEXT, '
                        'message TEXT, updated REAL, deadline REAL)')

    def begin(self, name, arguments, timeout=10):
        # Never store commands, source code, credentials, or full URLs in receipts.
        detail = arguments.get('filename') or arguments.get('path')
        label = name.replace('_', ' ')
        if detail:
            label += ': ' + str(detail).replace('\\', '/').rsplit('/', 1)[-1][:100]
        elif arguments.get('action'):
            label += ': ' + str(arguments['action'])[:40]
        operation = uuid4().hex[:12]
        now = time.time()
        self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                        (operation, name, label, 'running', '', now, now + timeout + 10))
        self.db.execute('DELETE FROM operations WHERE id NOT IN '
                        '(SELECT id FROM operations ORDER BY updated DESC LIMIT 100)')
        self.db.commit()
        return operation

    def finish(self, operation, state, message=''):
        self.db.execute('UPDATE operations SET state=?, message=?, updated=?, '
                        'deadline=CASE WHEN ?="waiting_approval" THEN ? ELSE deadline END WHERE id=?',
                        (state, str(message)[:700], time.time(), state, time.time() + 120, operation))
        self.db.commit()

    def resume(self, operation, timeout):
        now = time.time()
        self.db.execute('UPDATE operations SET state="running", message="", updated=?, deadline=? WHERE id=?',
                        (now, now + timeout + 10, operation))
        self.db.commit()

    def rows(self, tool=None, operation_id=None):
        query = 'SELECT id,tool,label,state,message,updated,deadline FROM operations'
        params = ()
        if operation_id:
            query += ' WHERE id=?'; params = (operation_id,)
        elif tool:
            query += ' WHERE tool=?'; params = (tool,)
        if not tool and not operation_id:
            query += " WHERE tool NOT LIKE '%status%' AND label NOT LIKE '%: status'"
        query += ' ORDER BY updated DESC LIMIT 20'
        names = ('id', 'tool', 'label', 'state', 'message', 'updated', 'deadline')
        rows = [dict(zip(names, row)) for row in self.db.execute(query, params)]
        for row in rows:
            if row['state'] in {'running', 'waiting_approval'} and time.time() > row['deadline']:
                row['state'] = 'unconfirmed' if row['state'] == 'running' else 'approval_expired'
        return rows

    def result(self, tool=None, operation_id=None):
        rows = self.rows(tool, operation_id)
        if not rows:
            return ToolResult(False, 'No actual execution is recorded for ' +
                              (tool.replace('_', ' ') if tool else 'that task') + '.', {'status': 'none'})
        row = rows[0]
        state = row['state']
        message = {
            'running': 'is running; completion has not been confirmed.',
            'waiting_approval': 'is waiting for approval; it has not started.',
            'approval_expired': 'did not start; its approval expired.',
            'unconfirmed': 'has no confirmed completion; the recorded execution may have been interrupted.',
            'cancelled': 'was cancelled.',
            'completed': 'completed.', 'failed': 'failed.',
            'submitted': 'was submitted; this is not confirmation that the background work finished.',
        }.get(state, 'has status ' + state + '.')
        text = row['label'] + ' ' + message
        if state in {'completed', 'failed', 'submitted'} and row['message']:
            text += ' ' + row['message']
        return ToolResult(state == 'completed', text, {'status': state, 'operation': row})


class CheckToolStatus:
    definition = ToolDefinition(
        name='check_tool_status',
        description='Check actual execution receipts for ANY tool. Supply a tool name or operation_id. Never infer task completion from conversation promises.',
        parameters={'type': 'object', 'properties': {
            'tool': {'type': 'string'}, 'operation_id': {'type': 'string'}},
            'additionalProperties': False})

    def __init__(self, store):
        self.store = store

    async def execute(self, arguments):
        return self.store.result(arguments.get('tool'), arguments.get('operation_id'))


def is_status_question(text):
    text = text.casefold()
    return bool(re.search(r'\b(?:is|was|has|have|did|are)\b.*\b(?:finished|done|complete|completed|sent|sending|running|progress|failed|opened)\b'
                          r'|\b(?:status|progress)\b|\b(?:have you sent|how much.*download|download.*speed)\b', text))


def topic_tool(text):
    text = text.casefold()
    for pattern, tool in (
        (r'\b(?:subagents?|agents?|background|extensive|delegat\w*)\b', 'agent_task'),
        (r'\b(?:transfer|upload|sent|sending|(?:send|give).*\b(?:file|computer|pc))\b', 'upload_to_pc'),
        (r'\bdownload\w*\b', 'download_file'),
        (r'\b(?:browser|chrome|google|webpage)\b', 'pc_browser'),
        (r'\b(?:command|powershell|cmd)\b', 'run_command'),
        (r'\b(?:program|python|coding|workspace|hello world)\b', 'coding_workspace'),
        (r'\b(?:search|browse|news)\b', 'search_web'),
        (r'\b(?:workflow|procedure|background task)\b', 'background_workflow'),
        (r'\bvpn\b', 'manage_vpn'),
        (r'\bweather\b', 'get_weather'),
        (r'\b(?:assignment|assignments)\b', 'teams_assignments'),
        (r'\b(?:channel|posts)\b', 'teams_channel_posts'),
        (r'\b(?:music|track|song)\b', 'netease_music'),
    ):
        if re.search(pattern, text):
            return tool
    return None
