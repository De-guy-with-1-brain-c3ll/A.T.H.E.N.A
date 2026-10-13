"""Verify real file reading, command execution, and writing through OpenClaw."""
import asyncio
import json

from athena.openclaw import installation, run_task
from athena.paths import PROJECT_ROOT


async def main():
    target = installation() / "workspace" / "setup-verification.txt"
    objective = (
        "Run this bounded setup verification using actual tools. "
        f"Read the first line of {PROJECT_ROOT / 'README.md'}. "
        "Execute node --version. "
        f"Write a UTF-8 file at {target} containing exactly these three lines: "
        "ATHENA_OPENCLAW_READY, then the README first line, then the actual node version. "
        "Read the saved file back and report the results. "
        "Do not modify any other project files, run installation commands, or create recurring jobs."
    )
    success, text, data = await run_task(objective)
    verified = False
    if target.is_file():
        lines = target.read_text(encoding="utf-8-sig").strip().splitlines()
        verified = len(lines) == 3 and lines[0] == "ATHENA_OPENCLAW_READY" and lines[1] == "# A.T.H.E.N.A." and lines[2].startswith("v26.")
    print(json.dumps({"reply_received": success, "file_verified": verified,
                      "reply": text, **data}, indent=2, ensure_ascii=False))
    return 0 if success and verified else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
