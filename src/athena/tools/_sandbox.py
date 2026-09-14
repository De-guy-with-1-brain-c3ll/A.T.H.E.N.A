"""Run untrusted Python only in a disposable, networkless sandbox."""
from __future__ import annotations
import asyncio
import json
import os
import shutil
from uuid import uuid4

IMAGE = "python:3.12-slim"
OUTPUT_LIMIT = 16000

# Input is a JSON snapshot, never a host mount. All program writes die with the container.
RUNNER = r'''
import json, os, pathlib, sys
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    resource.setrlimit(resource.RLIMIT_CPU, (35, 35))
except (ImportError, ValueError, OSError):
    pass
payload = json.load(sys.stdin)
os.chdir('/work')
for name, content in payload['files'].items():
    path = pathlib.Path(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid snapshot path')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
profile = payload['profile']
if profile == 'check':
    script = "import ast,pathlib; files=list(pathlib.Path('.').rglob('*.py')); assert files, 'No Python files'; [ast.parse(p.read_text(encoding='utf-8'),filename=str(p)) for p in files]; print('Syntax checks passed:',len(files),'files')"
    args = ['-c', script]
elif profile == 'test':
    script = "import sys,unittest; suite=unittest.defaultTestLoader.discover('.',pattern='test*.py'); count=suite.countTestCases(); print('Discovered tests:',count); result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(0 if count and result.wasSuccessful() else 1)"
    args = ['-c', script]
else:
    args = [payload['entrypoint'], *payload.get('args', [])]
os.execv(sys.executable, [sys.executable, '-u', '-B', *args])
'''


def docker_command(executable: str, name: str) -> list[str]:
    return [executable, "run", "--rm", "--pull=never", "--name", name,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=64",
            "--memory=256m", "--memory-swap=256m", "--cpus=1",
            "--user=65534:65534", "--workdir=/work",
            "--tmpfs=/work:rw,nosuid,nodev,size=32m,mode=1777",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--log-driver=none", "--env=PYTHONDONTWRITEBYTECODE=1",
            "--interactive", IMAGE, "python", "-u", "-c", RUNNER]


def bubblewrap_command(executable: str, python: str) -> list[str]:
    """Lightweight Linux sandbox suitable for a small ARM SBC."""
    return [executable, "--unshare-all", "--new-session", "--die-with-parent",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/work", "--tmpfs", "/tmp", "--chdir", "/work",
            "--setenv", "HOME", "/tmp", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            python, "-I", "-u", "-c", RUNNER]


async def _collect(stream):
    kept, size = bytearray(), 0
    while chunk := await stream.read(4096):
        size += len(chunk)
        remaining = OUTPUT_LIMIT - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
    return kept.decode("utf-8", errors="replace"), size > OUTPUT_LIMIT


async def run_snapshot(files: dict[str, str], profile: str,
                       entrypoint: str = "main.py", args=None, timeout: int = 20) -> dict:
    bwrap = shutil.which("bwrap") if os.name != "nt" else None
    python = shutil.which("python3") if bwrap else None
    docker = None if bwrap and python else shutil.which("docker")
    if bwrap and python:
        runtime = "bubblewrap"
        name = ""
        command = bubblewrap_command(bwrap, python)
    elif docker:
        runtime = "docker"
        name = "athena-job-" + uuid4().hex
        command = docker_command(docker, name)
    else:
        return {"success": False, "error": "sandbox_unavailable",
                "message": ("Install bubblewrap on Linux, or start Docker and pull python:3.12-slim. "
                            "No unsafe host execution fallback is allowed.")}
    process = await asyncio.create_subprocess_exec(*command,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output_task = asyncio.create_task(_collect(process.stdout))
    error_task = asyncio.create_task(_collect(process.stderr))
    timed_out = False
    try:
        async with asyncio.timeout(timeout):
            payload = json.dumps({"files": files, "profile": profile, "entrypoint": entrypoint,
                                  "args": args or []}).encode()
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
            await process.wait()
    except TimeoutError:
        timed_out = True
    finally:
        # Kill by random container name even if the Docker client was interrupted.
        async def cleanup():
            if runtime == "docker":
                cleanup_process = await asyncio.create_subprocess_exec(docker, "rm", "-f", name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                try:
                    await asyncio.wait_for(cleanup_process.wait(), timeout=5)
                except TimeoutError:
                    cleanup_process.kill()
                    await cleanup_process.wait()
            if process.returncode is None:
                process.kill()
            await process.wait()
            # Drain the final output after exit; don't discard a late failure message.
            await asyncio.wait({output_task, error_task}, timeout=2)
        try:
            await asyncio.shield(cleanup())
        finally:
            for task in (output_task, error_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(output_task, error_task, return_exceptions=True)
    stdout, out_cut = output_task.result() if not output_task.cancelled() else ("", True)
    stderr, err_cut = error_task.result() if not error_task.cancelled() else ("", True)
    return {"success": process.returncode == 0 and not timed_out,
            "exit_code": process.returncode, "timed_out": timed_out,
            "stdout": stdout, "stderr": stderr, "output_truncated": out_cut or err_cut,
            "profile": profile, "sandbox": runtime,
            "image": IMAGE if runtime == "docker" else None,
            "hint": "Docker must be running with the pre-pulled Linux image."
                    if runtime == "docker" and process.returncode == 125 else ""}
