"""Bounded local copies of public-web tool results for dashboard inspection."""
import json
import sqlite3
from datetime import datetime, timezone
from athena.paths import data_directory

def _connect():
    path = data_directory() / 'web-evidence.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=2)
    db.execute('CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY, payload TEXT)')
    return db

def record(tool, arguments, result, operation):
    row = {'tool': tool, 'operation_id': operation,
           'at': datetime.now(timezone.utc).isoformat(), 'success': result.success,
           'request': {k: arguments[k] for k in ('query', 'url', 'max_chars', 'limit') if k in arguments},
           'message': result.spoken_text, 'data': result.data}
    payload = json.dumps(row, ensure_ascii=False)
    if len(payload) > 60000:
        row['data'] = {'truncated_for_dashboard': True, 'raw_excerpt': json.dumps(result.data, ensure_ascii=False)[:50000]}
        payload = json.dumps(row, ensure_ascii=False)
    db = _connect()
    try:
        with db:
            db.execute('INSERT INTO evidence(payload) VALUES (?)', (payload,))
            db.execute('DELETE FROM evidence WHERE id NOT IN (SELECT id FROM evidence ORDER BY id DESC LIMIT 30)')
    finally:
        db.close()

def recent():
    db = _connect()
    try:
        return [json.loads(row[0]) for row in db.execute('SELECT payload FROM evidence ORDER BY id DESC LIMIT 30')]
    finally:
        db.close()
