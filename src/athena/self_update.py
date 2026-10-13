"""Isolated ATHENA improvements and explicitly requested installation with rollback."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import hashlib


async def git(root, *args):
    process = await asyncio.create_subprocess_exec('git', '-C', str(root), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    out, err = await process.communicate()
    if process.returncode:
        raise ValueError(err.decode(errors='replace')[:1000])
    return out.decode(errors='replace')


async def prepare(source, target, ident):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.exists():
        raise ValueError('Improvement worktree already exists; do not reset it automatically.')
    await git(source, 'worktree', 'add', '-b', 'codex/athena-' + ident, str(target), 'HEAD')
    # Include current application sources without copying credentials, personal
    # data or runtime state. User changes remain untouched in the main checkout.
    for relative in ('src', 'tools', 'tests', 'packages', 'openclaw-fork', 'pyproject.toml'):
        original = source / relative
        if original.is_dir():
            shutil.copytree(original, target / relative, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', 'build'))
        elif original.is_file():
            shutil.copy2(original, target / relative)
    await git(target, 'add', 'src', 'tools', 'tests', 'packages', 'openclaw-fork', 'pyproject.toml')
    baseline = await git(target, 'write-tree')
    return baseline.strip()


async def review(target, baseline):
    await git(target, 'add', 'src', 'tools', 'tests', 'packages', 'openclaw-fork', 'pyproject.toml')
    return await git(target, 'diff', '--cached', baseline, '--', 'src', 'tools', 'tests', 'packages', 'openclaw-fork', 'pyproject.toml')


def install(source, target, diff, backup, verified_tree):
    """Called only by an explicit console install action after review/tests."""
    source, target, backup = Path(source).resolve(), Path(target).resolve(), Path(backup).resolve()
    result = subprocess.run(['git', '-C', str(target), 'diff', '--cached', '--name-only', diff],
                            capture_output=True, text=True, check=True)
    changed = []
    for relative in result.stdout.splitlines():
        destination = (source / relative).resolve()
        if not destination.is_relative_to(source) or not relative.startswith(('src/', 'tools/', 'tests/', 'packages/', 'openclaw-fork/')) and relative != 'pyproject.toml':
            raise ValueError('Update includes an unsupported path.')
        if not (target / relative).is_file():
            raise ValueError('File deletion requires manual review; installation was not started.')
        baseline = subprocess.run(['git', '-C', str(target), 'show', diff + ':' + relative], capture_output=True)
        if baseline.returncode == 0:
            if not destination.is_file() or destination.read_bytes().replace(b'\r\n', b'\n') != baseline.stdout.replace(b'\r\n', b'\n'):
                raise ValueError('Live ATHENA files changed during the task; installation would overwrite newer work.')
        elif destination.exists():
            raise ValueError('A new live file conflicts with this improvement; review before installing.')
        changed.append(relative)
    staged_tree = subprocess.run(['git', '-C', str(target), 'write-tree'], capture_output=True, text=True, check=True).stdout.strip()
    if staged_tree != verified_tree:
        raise ValueError('Staged content changed after verification; review and verify again.')
    blobs = {}
    for relative in changed:
        # Install exactly the staged/reviewed content; the mutable worktree is
        # never the installation source after verification.
        blob = subprocess.run(['git', '-C', str(target), 'show', staged_tree + ':' + relative],
                              capture_output=True, check=True).stdout
        if (target / relative).read_bytes().replace(b'\r\n', b'\n') != blob.replace(b'\r\n', b'\n'):
            raise ValueError('Improvement files changed after staging; review and verify again.')
        blobs[relative] = blob
    backup.mkdir(parents=True, exist_ok=False)
    records = []
    try:
        for relative in changed:
            destination = source / relative
            exists = destination.exists()
            saved = backup / relative
            if exists:
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
            records.append({'path': relative, 'existed': exists})
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(blobs[relative])
        (backup / 'manifest.json').write_text(json.dumps(records))
    except BaseException:
        rollback(source, backup, records)
        raise
    return records


def rollback(source, backup, records=None):
    source, backup = Path(source).resolve(), Path(backup).resolve()
    records = records or json.loads((backup / 'manifest.json').read_text())
    for row in reversed(records):
        destination = (source / row['path']).resolve()
        if not destination.is_relative_to(source): raise ValueError('Invalid rollback path.')
        if row['existed']:
            shutil.copy2(backup / row['path'], destination)
        elif destination.is_file():
            destination.unlink()
