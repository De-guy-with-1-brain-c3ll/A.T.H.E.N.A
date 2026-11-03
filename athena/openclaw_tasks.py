"""Durable task receipts and read-only progress from the pinned OpenClaw runtime."""
import asyncio
from contextlib import closing
import json
import os
import sqlite3
import time
from uuid import uuid4
from athena.openclaw import installation, run_task, redact, environment


def session_progress(session):
    """Read only this ATHENA-created session; never expose tool arguments or output."""
    path = installation() / 'state/agents/main/agent/openclaw-agent.sqlite'
    events = []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)) as db:
            row = db.execute('SELECT current_session_id FROM session_nodes WHERE session_key=?',
                             ('agent:main:' + session,)).fetchone()
            if not row:
                return events
            for seq, raw in db.execute('SELECT seq,event_json FROM transcript_events WHERE session_id=? '
                                      'ORDER BY seq DESC LIMIT 30', (row[0],)):
                message = json.loads(raw).get('message', {})
                if message.get('role') == 'toolResult':
                    events.append({'seq': seq, 'tool': str(message.get('toolName', 'tool'))[:80],
                                   'state': 'failed' if message.get('isError') else 'returned'})
                elif message.get('role') == 'assistant':
                    for item in message.get('content', []):
                        if isinstance(item, dict) and item.get('type') == 'toolCall':
                            events.append({'seq': seq, 'tool': str(item.get('name', 'tool'))[:80], 'state': 'called'})
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        pass  # A runtime schema change removes detail, never invents progress.
    return events[:10]


class OpenClawTasks:
    def __init__(self, path=None):
        path = path or installation() / 'athena-tasks.sqlite'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS tasks '
            '(id TEXT PRIMARY KEY, session TEXT, state TEXT, report TEXT, created REAL, updated REAL, pid INTEGER)')
        self.db.commit()
        self.running = set()

    def start(self, objective, ident=None):
        ident = ident or uuid4().hex[:12]
        old = self.db.execute('SELECT id FROM tasks WHERE id=?', (ident,)).fetchone()
        if old:
            return ident  # Same submission ID never starts the task twice.
        now = time.time()
        session = 'athena-' + uuid4().hex
        self.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)',
                        (ident, session, 'queued', '', now, now, os.getpid()))
        self.db.commit()
        task = asyncio.create_task(self._run(ident, session, objective))
        self.running.add(task)
        task.add_done_callback(self.running.discard)
        return ident

    def update(self, ident, state, report=''):
        self.db.execute('UPDATE tasks SET state=?,report=?,updated=? WHERE id=?',
                        (state, report[:16000], time.time(), ident))
        self.db.commit()

    async def _run(self, ident, session, objective):
        self.update(ident, 'running')
        try:
            success, report, _ = await run_task(objective, session=session)
            self.update(ident, 'reply_received' if success else 'unconfirmed', report)
        except asyncio.CancelledError:
            self.update(ident, 'unconfirmed', 'The ATHENA caller stopped; OpenClaw may still be working. Do not restart this task automatically.')
            raise
        except TimeoutError:
            self.update(ident, 'unconfirmed', 'The reply timed out; OpenClaw may still be working. Inspect its session before retrying.')
        except Exception as error:
            self.update(ident, 'unconfirmed', f'No confirmed agent reply ({type(error).__name__}). Inspect its session before retrying.')

    def rows(self, ident=None, report=False):
        query = 'SELECT * FROM tasks' + (' WHERE id=?' if ident else '') + ' ORDER BY created DESC LIMIT 10'
        rows = []
        for record in self.db.execute(query, (ident,) if ident else ()):
            row = dict(record)
            if row['state'] in {'queued', 'running'}:
                import psutil
                try:
                    alive = psutil.Process(row['pid']).create_time() <= row['created'] + .1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    alive = False
                if not alive:
                    row['state'] = 'unconfirmed'
                    row['report'] = 'The task worker is no longer running; completion is unconfirmed.'
            events = session_progress(row['session'])
            row['events'] = events
            row['progress'] = (f"Latest recorded tool: {events[0]['tool']} ({events[0]['state']})."
                               if events else 'No tool activity recorded yet.')
            row['session_key'] = row.pop('session')
            row.pop('pid')
            row['report'] = redact(row['report'], environment()) if report else ''
            rows.append(row)
        return rows


_managers = {}


def task_manager():
    key = str(installation().resolve())
    if key not in _managers:
        _managers[key] = OpenClawTasks()
    return _managers[key]
