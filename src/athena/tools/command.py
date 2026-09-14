"""Approval-gated host command execution for local automation."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import subprocess

from athena.tools.models import PermissionLevel, ToolDefinition, ToolResult
from athena.paths import data_directory


def windows_desktop() -> Path:
    """Return the configured Desktop, including OneDrive redirection."""
    if os.name == "nt":
        try:
            import winreg
            key_name = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                configured, _ = winreg.QueryValueEx(key, "Desktop")
            path = Path(os.path.expandvars(str(configured))).expanduser()
            if path.is_absolute():
                return path
        except (OSError, ValueError):
            pass
    return Path.home() / "Desktop"


class CommandTool:
    definition = ToolDefinition(
        name="run_command",
        description=(
            "Run a Windows PowerShell/CMD or Linux bash command on the current host after the hub "
            "shows the exact command and receives user approval. Use for local computer "
            "automation requested by the user. Never claim it ran before the tool result. "
            "On a headless Linux service, use the writable automation folder rather than desktop. "
            "Do not use it to download files; use download_file instead."
        ),
        parameters={"type": "object", "properties": {
            "command": {"type": "string", "minLength": 1, "maxLength": 2000},
            "shell": {"type": "string", "enum": ["powershell", "cmd", "bash"]},
            "cwd": {"type": "string", "enum": ["project", "automation", "desktop"]},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
        }, "required": ["command", "shell"], "additionalProperties": False},
        permission=PermissionLevel.CONFIRM,
        timeout_seconds=130,
    )

    def __init__(self, project_root: Path | None = None, desktop_root: Path | None = None):
        self.project_root = (project_root or Path(__file__).resolve().parents[3]).resolve()
        self.automation_root = data_directory() / "automation"
        self.desktop_root = (desktop_root or windows_desktop()).resolve()

    def prepare(self, arguments):
        command = arguments["command"].strip()
        shell = arguments["shell"]
        self._validate(command, shell)
        actual = {
            "command": command,
            "shell": shell,
            "cwd": arguments.get("cwd", "project"),
            "timeout_seconds": arguments.get("timeout_seconds", 60),
        }
        locations = {"project": "ATHENA project", "automation": "automation folder",
                     "desktop": "Windows Desktop" if os.name == "nt" else "Desktop folder"}
        location = locations[actual["cwd"]]
        return ToolResult(True,
            f"Run this {shell} command in the {location}?\n{command}\nSay yes to approve, or no to cancel.",
            {"arguments": actual})

    async def execute(self, arguments):
        command = arguments["command"].strip()
        shell = arguments["shell"]
        self._validate(command, shell)
        location = arguments.get("cwd", "project")
        cwd = {"project": self.project_root, "automation": self.automation_root,
               "desktop": self.desktop_root}[location]
        if location == "desktop" and not cwd.is_dir():
            label = "Windows Desktop" if os.name == "nt" else "Desktop folder"
            return ToolResult(False, f"I couldn't locate the {label}.",
                              {"status": "failed"})
        cwd.mkdir(parents=True, exist_ok=True)
        env = {key: value for key, value in os.environ.items()
               if not re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I)}
        if shell == "bash":
            executable = Path("/bin/bash")
            argv = [str(executable), "--noprofile", "--norc", "-c", command]
        elif shell == "cmd":
            executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
            argv = [str(executable), "/d", "/s", "/c", command]
        else:
            executable = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" /
                          "WindowsPowerShell" / "v1.0" / "powershell.exe")
            argv = [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=flags,
        )
        timeout = arguments.get("timeout_seconds", 60)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult(False, f"The command was stopped after {timeout} seconds.",
                              {"status": "timed_out", "exit_code": None})
        out = stdout.decode(errors="replace")[-20_000:].strip()
        err = stderr.decode(errors="replace")[-20_000:].strip()
        success = process.returncode == 0
        summary = f"Command finished with exit code {process.returncode}."
        if out:
            summary += f"\nOutput:\n{out}"
        if err:
            summary += f"\nError output:\n{err}"
        return ToolResult(success, summary, {"status": "complete" if success else "failed",
            "exit_code": process.returncode, "stdout": out, "stderr": err,
            "untrusted_content": True})

    @staticmethod
    def _validate(command: str, shell: str):
        lowered = command.casefold()
        if os.name == "nt" and shell == "bash":
            raise ValueError("Use PowerShell or CMD on this Windows host.")
        if os.name != "nt" and shell != "bash":
            raise ValueError("Use bash on this Linux host.")
        if "\n" in command or "\r" in command:
            raise ValueError("Run one visible command at a time; line breaks are not allowed.")
        if re.search(r"(?:-encodedcommand|-enc\b|frombase64string|certutil\s+-decode)", lowered):
            raise ValueError("Encoded or hidden commands are not allowed.")
        if (shell == "cmd" and re.search(r"\bpowershell(?:\.exe)?\b", lowered)) or (
                shell == "powershell" and re.search(r"\bcmd(?:\.exe)?\s+/[ck]\b", lowered)):
            raise ValueError("Nested shells are not allowed; submit the real command directly.")
        if re.search(r"\b(curl|wget|bitsadmin)\b|invoke-webrequest|start-bitstransfer|certutil\s+-urlcache", lowered):
            raise ValueError("Use ATHENA's inspected, approval-gated download tool for internet files.")
        if re.search(r"(?:\.env\b|credentials?|api[_ -]?keys?|credential manager|cmdkey|vaultcmd)", lowered):
            raise ValueError("Commands may not read credential or secret stores.")
        dangerous = (
            r"\b(format|diskpart|bcdedit)\b|\breg\s+delete\b|"
            r"\b(shutdown|restart-computer|stop-computer)\b|"
            r"\b(del|erase)\b[^\r\n]*(?:/s|/q)|\b(?:rd|rmdir)\b[^\r\n]*/s|"
            r"remove-item[^\r\n]*-recurse|clear-disk|remove-partition|"
            r"\b(?:sudo|su)\b|\b(?:reboot|poweroff|halt|mkfs(?:\.[a-z0-9]+)?)\b|"
            r"\bsystemctl\s+(?:poweroff|reboot|halt)\b|"
            r"\brm\b[^\r\n]*(?:\s-(?:[^\s]*r[^\s]*f|[^\s]*f[^\s]*r)|\s--recursive|\s--force)"
        )
        if re.search(dangerous, lowered):
            raise ValueError("That command is too destructive for ATHENA's automation runner.")


def create_tools():
    return [CommandTool()]
