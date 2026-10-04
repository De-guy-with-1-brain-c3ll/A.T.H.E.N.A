"""Send a harmless synthetic receipt test from the Pi to its configured PC."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from athena.config import load_local_environment
from athena.paths import data_directory
from athena.tools.pc_transfer import UploadTool


async def main():
    load_local_environment()
    root = data_directory() / "reports"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"connection-test-{uuid4().hex[:8]}.txt"
    path.write_text("ATHENA Pi-to-PC connection test. No private data.\n", encoding="utf-8")
    try:
        tool = UploadTool()
        approved, _ = await tool.prepare({"path": str(path)})
        result = await tool.execute(approved)
        print(result.spoken_text)
        if not result.success: raise SystemExit(1)
        print("Verified receipt:", result.data["filename"], result.data["bytes"], "bytes")
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__": asyncio.run(main())
