"""Operational task index and replayable events, separate from personal memory."""
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


class TaskStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, task TEXT NOT NULL);''')
        self.db.execute('CREATE TABLE IF NOT EXISTS routines (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS conversation_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)')
        self.db.commit()

    def create(self, objective, cwd, worker, request_id, session='console', require_approval=True):
        previous = self.db.execute('SELECT task FROM requests WHERE id=?', (request_id,)).fetchone()
        if previous:
            return self.get(previous[0]), False
        ident = uuid4().hex[:12]
        body = {'id': ident, 'objective': objective, 'cwd': str(Path(cwd).resolve()),
                'worker': worker, 'session': session, 'state': 'awaiting_approval' if require_approval else 'queued', 'created': time.time(),
                'updated': time.time(), 'run_id': None, 'worker_session': 'agent:athena-worker:' + ident,
                'report': '', 'verification': [], 'artifacts': [], 'approval_id': ident if require_approval else None,
                'guidance': [], 'generation': 0}
        with self.db:
            self.db.execute('INSERT INTO tasks VALUES (?,?)', (ident, json.dumps(body)))
            self.db.execute('INSERT INTO requests VALUES (?,?)', (request_id, ident))
        self.event(ident, 'approval' if require_approval else 'prepared', {'objective': objective, 'approval_id': ident if require_approval else None})
        return body, True

    def get(self, ident):
        row = self.db.execute('SELECT body FROM tasks WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ValueError('Task not found: ' + ident)
        return json.loads(row[0])

    def rows(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM tasks ORDER BY rowid DESC LIMIT 100')]

    def update(self, ident, **values):
        body = self.get(ident)
        body.update(values, updated=time.time())
        with self.db:
            self.db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(body), ident))
        return body

    def event(self, ident, kind, data):
        body = {'type': kind, 'data': data, 'time': time.time()}
        with self.db:
            cursor = self.db.execute('INSERT INTO events(task,body) VALUES (?,?)', (ident, json.dumps(body)))
        return dict(body, seq=cursor.lastrowid, task_id=ident)

    def events(self, ident=None, after=0):
        query = 'SELECT seq,task,body FROM events WHERE seq>?'
        args = [after]
        if ident:
            query += ' AND task=?'
            args.append(ident)
        return [dict(json.loads(row[2]), seq=row[0], task_id=row[1])
                for row in self.db.execute(query + ' ORDER BY seq LIMIT 500', args)]

    def close(self):
        self.db.close()

    def conversation(self, role, text, session, mode, request_id):
        with self.db:
            self.db.execute('INSERT INTO conversation_log(body) VALUES (?)',
                (json.dumps({'role':role,'text':text,'session':session,'mode':mode,
                    'request_id':request_id,'time':time.time()}),))
            self.db.execute('DELETE FROM conversation_log WHERE seq NOT IN (SELECT seq FROM conversation_log ORDER BY seq DESC LIMIT 5000)')

    def conversation_tail(self, limit=40):
        rows = self.db.execute('SELECT seq,body FROM conversation_log ORDER BY seq DESC LIMIT ?', (limit,)).fetchall()
        return [dict(json.loads(r['body']),seq=r['seq']) for r in reversed(rows)]

    def routine(self, ident, body=None):
        if body is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO routines VALUES (?,?)', (ident, json.dumps(body)))
            return body
        row = self.db.execute('SELECT body FROM routines WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else None
