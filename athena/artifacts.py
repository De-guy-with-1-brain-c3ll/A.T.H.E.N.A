"""Small shared catalogue of files actually saved by ATHENA tools."""
import json
import time
from pathlib import Path
from uuid import uuid4

from athena.paths import data_directory


def saved_artifacts(root=None):
    root = Path(root) if root is not None else data_directory()
    try:
        rows = json.loads((root / 'saved-artifacts.json').read_text(encoding='utf-8'))
        return [row for row in rows if isinstance(row, dict)
                and isinstance(row.get('path'), str) and Path(row['path']).is_file()][:20]
    except (OSError, ValueError, TypeError):
        return []


def remember_artifact(path, root=None):
    from athena.alerts import _FileLock
    path = Path(path).absolute()
    root = Path(root) if root is not None else data_directory()
    root.mkdir(parents=True, exist_ok=True)
    with _FileLock(root / 'saved-artifacts.lock'):
        rows = [row for row in saved_artifacts(root) if row['path'] != str(path)]
        rows.insert(0, {'path': str(path), 'bytes': path.stat().st_size, 'saved_at': time.time()})
        temporary = root / f'.artifacts-{uuid4().hex}.tmp'
        try:
            temporary.write_text(json.dumps(rows[:20]), encoding='utf-8')
            temporary.replace(root / 'saved-artifacts.json')
        finally:
            temporary.unlink(missing_ok=True)
