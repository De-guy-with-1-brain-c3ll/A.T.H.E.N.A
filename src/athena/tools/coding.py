"""Create and run programs inside a dedicated, isolated workspace."""
from __future__ import annotations
import asyncio
from pathlib import Path
import re
from athena.tools._sandbox import run_snapshot
from athena.tools.models import ToolDefinition, ToolResult
from athena.paths import data_directory

ALLOWED_EXTENSIONS = {".py", ".md", ".txt", ".json", ".toml", ".cfg", ".ini", ".csv", ".html", ".css", ".js", ".sql"}
MAX_FILE_BYTES = 60000
MAX_PROJECT_BYTES = 1_000_000


def check_link(path: Path):
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
        raise ValueError("Links and junctions are not allowed in coding workspaces.")


class CodingWorkspaceTool:
    definition = ToolDefinition(
        name="coding_workspace",
        description=("Write, implement, inspect and test Python programs. Actions: create project; list files; write UTF-8 file; read file; check Python syntax; test using unittest; run Python entrypoint. "
                     "Only dedicated scratch projects, never the live hub or existing user files. Execution uses Bubblewrap on Linux or Docker on Windows; no network or host access. "
                     "Write main.py and test_*.py, run test, inspect errors and fix. Only standard-library Python dependencies are installed. Writing files is not proof tests passed."),
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "list", "write", "read", "check", "test", "run"]},
            "project": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$"},
            "path": {"type": "string", "minLength": 1, "maxLength": 180},
            "content": {"type": "string", "maxLength": 50000},
            "args": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 10},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 30, "default": 20},
        }, "required": ["action", "project"], "additionalProperties": False}, timeout_seconds=40)

    def __init__(self, root: Path | None = None, runner=None):
        self.root = root or data_directory() / "coding"
        self.runner = runner or run_snapshot
        self.lock = asyncio.Lock()

    def project_path(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", name):
            raise ValueError("Invalid project name.")
        for ancestor in (*self.root.parents, self.root):
            check_link(ancestor)
        project = self.root / name
        check_link(project)
        return project

    def file_path(self, project: Path, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", name):
            raise ValueError("Use a relative path with plain letters, numbers, dots, underscores and slashes.")
        parts = name.split("/")
        if any(not p or p in {".", ".."} or p.startswith(".") for p in parts):
            raise ValueError("Hidden files and traversal are forbidden.")
        if any(p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]} for p in parts):
            raise ValueError("Reserved filename.")
        path = project.joinpath(*parts)
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError("Unsupported source-file extension.")
        cursor = project
        for part in parts:
            cursor /= part
            check_link(cursor)
        if not path.resolve().is_relative_to(project.resolve()):
            raise ValueError("Path escapes the project.")
        return path

    def snapshot(self, project: Path) -> dict[str, str]:
        files, size = {}, 0
        for path in project.rglob("*"):
            check_link(path)
            if not path.is_file():
                continue
            relative = path.relative_to(project).as_posix()
            self.file_path(project, relative)
            size += path.stat().st_size
            if path.stat().st_size > MAX_FILE_BYTES or size > MAX_PROJECT_BYTES or len(files) >= 100:
                raise ValueError("Project exceeds the 100-file/1 MB limit or a file exceeds 60 KB.")
            files[relative] = path.read_text(encoding="utf-8")
        return files

    async def execute(self, arguments: dict) -> ToolResult:
        async with self.lock:
            try:
                project = self.project_path(arguments["project"])
                action = arguments["action"]
                if action == "create":
                    project.mkdir(parents=True, exist_ok=True)
                    return ToolResult(True, "The coding workspace is ready.", {"project": arguments["project"], "workspace": str(project)})
                if not project.is_dir():
                    return ToolResult(False, "Create the coding project first.")
                files = self.snapshot(project)
                if action == "list":
                    return ToolResult(True, "These are the project files.", {"files": sorted(files)})
                if action in {"write", "read"}:
                    if "path" not in arguments:
                        return ToolResult(False, "A file path is required.")
                    path = self.file_path(project, arguments["path"])
                    if action == "read":
                        return ToolResult(True, "I read the project file.", {"path": arguments["path"], "content": path.read_text(encoding="utf-8"), "untrusted_content": True})
                    if "content" not in arguments:
                        return ToolResult(False, "File content is required.")
                    content = arguments["content"]
                    files[arguments["path"]] = content
                    if (len(content.encode()) > MAX_FILE_BYTES or len(files) > 100
                            or sum(len(v.encode()) for v in files.values()) > MAX_PROJECT_BYTES):
                        return ToolResult(False, "The file or project size limit would be exceeded.")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                    return ToolResult(True, "I saved the program file; it has not been tested yet.", {"path": str(path), "bytes": len(content.encode()), "tested": False})
                if action not in {"check", "test", "run"}:
                    return ToolResult(False, "Unsupported coding action.")
                entrypoint = arguments.get("path", "main.py")
                if action == "run":
                    path = self.file_path(project, entrypoint)
                    if path.suffix != ".py" or entrypoint not in files:
                        return ToolResult(False, "The entrypoint must be an existing Python file.")
                result = await self.runner(files, action, entrypoint, arguments.get("args", []), arguments.get("timeout", 20))
                passed = bool(result.get("success"))
                result["untrusted_content"] = True
                return ToolResult(passed,
                    f"The {action} completed successfully." if passed else result.get("message", f"The {action} failed. Inspect the test output before claiming success."), result)
            except (ValueError, OSError, UnicodeError) as error:
                return ToolResult(False, f"Coding request rejected: {error}")


def create_tools():
    return [CodingWorkspaceTool()]
