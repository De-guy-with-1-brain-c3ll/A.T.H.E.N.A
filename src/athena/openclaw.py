"""Run ATHENA's private OpenClaw installation without global PATH changes."""
from __future__ import annotations

import asyncio
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from athena.config import load_local_environment
from athena.paths import data_directory, environment_path


def installation() -> Path:
    return environment_path('ATHENA_OPENCLAW_DIR', data_directory() / 'openclaw')


def node_path() -> Path:
    root = installation()
    preferred = root / "node-v26.11.1-win-x64" / "node.exe"
    return preferred if preferred.is_file() else root / "node-v24.19.0-win-x64" / "node.exe"


def command() -> list[str]:
    root = installation()
    node = node_path()
    source = root / 'source'
    marker = source / '.athena-build-ready.json'
    if os.environ.get('ATHENA_OPENCLAW_RUNTIME') != 'stable' and marker.is_file():
        built = json.loads(marker.read_text())
        entry = source / 'dist/entry.js'
        if (built.get('fullBuildPassed') is True and built.get('version') == '2026.8.1'
                and built.get('commit') == 'ea806575e6450e4d1efdfc72c19f04be982a1b9b'
                and hashlib.sha256(entry.read_bytes()).hexdigest() == built.get('entrySha256')):
            return [str(node), str(source / 'openclaw.mjs')]
        raise FileNotFoundError('The source runtime build did not match its verified marker. Use ATHENA_OPENCLAW_RUNTIME=stable for rollback.')
    runtime = root / "runtime-stable"
    if not (runtime / "node_modules" / "openclaw" / "openclaw.mjs").is_file():
        runtime = root / "runtime"
    entry = runtime / "node_modules" / "openclaw" / "openclaw.mjs"
    if not node.is_file() or not entry.is_file():
        raise FileNotFoundError("OpenClaw is not installed on this ATHENA computer.")
    return [str(node), str(entry)]


def environment() -> dict[str, str]:
    load_local_environment()
    env = os.environ.copy()
    if env.get("DEEPSEEK_API_KEY"):
        env["CUSTOM_API_KEY"] = env["DEEPSEEK_API_KEY"]
    # The custom route needs just this credential. Ambient ATHENA channel/provider
    # keys otherwise trigger OpenClaw's automatic plugin onboarding.
    for name in list(env):
        if name.startswith(("FEISHU_", "DASHSCOPE_", "DEEPSEEK_")):
            env.pop(name)
    root = installation()
    env["OPENCLAW_STATE_DIR"] = str(root / "state")
    env["OPENCLAW_CONFIG_PATH"] = str(root / "state" / "openclaw.json")
    env["OPENCLAW_DISABLE_BONJOUR"] = "1"
    env["OPENCLAW_EXEC_SHELL_SNAPSHOT"] = "0"
    env["OPENCLAW_NO_RESPAWN"] = "1"
    env["OPENCLAW_SKIP_CHANNELS"] = "1"
    env["PATH"] = str(node_path().parent) + os.pathsep + env.get("PATH", "")
    return env


def redact(text: str, env: dict[str, str]) -> str:
    for name, value in env.items():
        if value and len(value) >= 8 and any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[redacted]")
    return text


async def run_task(objective: str, timeout: int = 300, session: str | None = None) -> tuple[bool, str, dict]:
    """Submit one task; never retry ambiguous failures that could repeat actions."""
    env = environment()
    session = session or "athena-" + uuid4().hex
    source = installation().parent.parent
    python = source / '.venv/Scripts/python.exe'
    locations = ('ATHENA task environment (verified local paths, not profile memory):\n'
                 f'ATHENA source: {source}\n'
                 f'Python executable: {python if python.is_file() else sys.executable}\n'
                 f'OpenClaw workspace: {installation() / "workspace"}\n'
                 'Use these actual paths rather than stale paths in earlier instructions.\n\n')
    args = command() + ["agent", "--agent", "main", "--session-key", session,
                        "--message", locations + objective, "--timeout", str(timeout), "--json"]
    process = await asyncio.create_subprocess_exec(
        *args, cwd=str(installation()), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout + 30)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    output = redact(stdout.decode("utf-8", errors="replace"), env)
    if process.returncode:
        return False, "OpenClaw did not confirm completion. Check its session before retrying.", {"session_key": session, "diagnostic": redact(stderr.decode("utf-8", errors="replace"), env)[-2000:]}
    try:
        body = json.loads(output)
    except json.JSONDecodeError:
        return False, "OpenClaw returned an unreadable result; completion is unverified.", {"session_key": session}
    if not isinstance(body, dict):
        return False, "OpenClaw returned an unexpected result; completion is unverified.", {"session_key": session}
    result = body.get("result", body)
    if not isinstance(result, dict):
        return False, "OpenClaw returned an unexpected result; completion is unverified.", {"session_key": session}
    texts = [item["text"] for item in result.get("payloads", []) if isinstance(item, dict) and isinstance(item.get("text"), str)]
    if not texts or body.get("status") in {"error", "pending"}:
        return False, "OpenClaw has no confirmed final reply. Inspect its session before retrying.", {"session_key": session}
    return True, "\n".join(texts), {"session_key": session, "reply_received": True}


def main() -> int:
    try:
        return subprocess.call(command() + sys.argv[1:], cwd=str(installation()), env=environment())
    except (FileNotFoundError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
