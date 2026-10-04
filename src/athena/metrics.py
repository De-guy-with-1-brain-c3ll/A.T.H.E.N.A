"""Small cross-process counters. No paid calls, audio recordings or credentials."""
import json
import sqlite3
import time
from contextlib import closing
from athena.paths import data_directory


def record(category, values):
    try:
        root = data_directory(); root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(root / 'metrics.sqlite3', timeout=.1)) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS metrics (category TEXT, payload TEXT, at REAL)')
                db.execute('INSERT INTO metrics VALUES (?,?,?)', (category, json.dumps(values), time.time()))
                db.execute('DELETE FROM metrics WHERE rowid NOT IN (SELECT rowid FROM metrics ORDER BY rowid DESC LIMIT 3000)')
    except (OSError, sqlite3.Error):
        pass  # Optional monitoring must not break a conversation.


def snapshot():
    totals = {}; latest = {}
    path = data_directory() / 'metrics.sqlite3'
    if not path.exists():
        return {'totals': {}, 'latest': {}, 'scope': 'last 3000 recorded events; not an invoice'}
    try:
        with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=.1)) as db:
            for category, payload, at in db.execute('SELECT category,payload,at FROM metrics ORDER BY rowid'):
                values = json.loads(payload)
                latest[category] = {**values, 'at': at}
                group = totals.setdefault(category, {})
                for name, value in values.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        group[name] = group.get(name, 0) + value
    except (OSError, ValueError, sqlite3.Error):
        pass
    return {'totals': totals, 'latest': latest, 'scope': 'last 3000 recorded events; not an invoice'}


def progress(name, values):
    """Publish a sanitized latest transfer/download sample atomically."""
    from uuid import uuid4
    try:
        root = data_directory(); root.mkdir(parents=True, exist_ok=True)
        target = root / (name + '-progress.json')
        temporary = root / ('.progress-' + uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps({**values, 'updated': time.time()}), encoding='utf-8')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        pass


def read_progress(name):
    try:
        return json.loads((data_directory() / (name + '-progress.json')).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
