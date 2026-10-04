"""Verify real tool discovery, ordered execution and reports without an AI call."""
import asyncio
from pathlib import Path
import tempfile

from athena.config import load_local_environment
from athena.services import build_registry
from athena.settings.store import RuntimeSettingsStore
from athena.workflows import Workflows


async def main():
    load_local_environment()
    registry, alerts = build_registry(RuntimeSettingsStore())
    try:
        with tempfile.TemporaryDirectory() as directory:
            reports = []
            manager = Workflows(registry, lambda text: reports.append(text) or True,
                                Path(directory)/"workflow.sqlite3")
            registry.get("background_workflow").bind({"workflows": manager})
            result = await registry.execute("background_workflow", {
                "action": "create", "title": "Local time validation",
                "steps": [{"tool": "get_local_time", "arguments": {"timezone": "Asia/Shanghai"}}] * 2})
            assert result.success, result.spoken_text
            await manager.run(manager.claim())
            await manager.report()
            rows = manager.rows(result.data["task_id"])
            assert rows[0]["state"] == "complete" and rows[0]["completed_steps"] == 2, rows
            assert len(reports) == 1, reports
            print("Real registry workflow: two ordered steps completed, one proactive report accepted.")
    finally:
        await alerts.close()
        await registry.close()


if __name__ == "__main__": asyncio.run(main())
