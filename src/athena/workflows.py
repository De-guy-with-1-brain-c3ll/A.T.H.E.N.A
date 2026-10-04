"""Durable, cross-process background procedures; no model polling."""
from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from athena.paths import data_directory
from athena.tools.models import PermissionLevel

# Only explicitly reviewed tools can run unattended. Approval-gated actions,
# shutdown, prompt changes and recursive workflow creation are never inherited.
ALLOWED = {"search_web", "browse_webpage", "get_weather", "get_local_time",
           "teams_assignments", "teams_channel_posts", "teams_channels",
           "coding_workspace"}


class Workflows:
    def __init__(self, registry, notify, path=None):
        self.registry, self.notify = registry, notify
        self.path = Path(path or data_directory() / "workflows.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, title TEXT, steps TEXT, state TEXT,
                position INTEGER, results TEXT, due REAL, interval INTEGER,
                lease REAL DEFAULT 0, report TEXT DEFAULT '', notified INTEGER DEFAULT 0)""")
        self.task = None

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, title, steps, interval=0):
        if not 1 <= len(steps) <= 12 or interval and interval < 300:
            raise ValueError("Use 1–12 steps and repeat intervals of at least five minutes.")
        for step in steps:
            name = step["tool"]
            tool = self.registry.get(name)
            if name not in ALLOWED or tool is None or tool.definition.permission != PermissionLevel.SAFE:
                raise ValueError(f"{name} cannot run unattended; ask for it separately.")
            if name == "coding_workspace" and step["arguments"].get("action") == "run":
                raise ValueError("Unattended arbitrary program execution is not allowed; use sandboxed test/check.")
            self.registry._validate_arguments(tool.definition.parameters, step["arguments"])
        ident = uuid4().hex[:10]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""DELETE FROM jobs WHERE interval=0 AND notified=1 AND
                state IN ('complete','failed','cancelled') AND id NOT IN
                (SELECT id FROM jobs ORDER BY due DESC LIMIT 200)""")
            if db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= 500:
                raise ValueError("Task history is full; deliver pending reports or remove unused tasks first.")
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running') OR interval>0").fetchone()[0] >= 40:
                raise ValueError("Forty active procedures already exist; cancel unused ones first.")
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (ident, title[:160], json.dumps(steps), "queued", 0, "[]",
                        time.time(), interval, 0, "", 0))
        return ident

    def rows(self, ident=None):
        with self.db() as db:
            rows = db.execute("SELECT * FROM jobs WHERE id=?" if ident else
                              "SELECT * FROM jobs ORDER BY due DESC LIMIT 10",
                              (ident,) if ident else ()).fetchall()
        return [{"id": r["id"], "title": r["title"], "state": r["state"],
                 "completed_steps": r["position"], "total_steps": len(json.loads(r["steps"])),
                 "repeat_seconds": r["interval"], "report": r["report"][:500]} for r in rows]

    def cancel(self, ident):
        with self.db() as db:
            return db.execute("UPDATE jobs SET state='cancelled', interval=0, notified=1, report='' WHERE id=?",
                              (ident,)).rowcount > 0

    def export(self, ident):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("Task not found.")
        root = data_directory() / "reports"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"task-{row['id']}.json"
        from athena.tools.coding import check_link
        for parent in (*path.parents, path):
            check_link(parent)
        path.write_text(json.dumps({"id": row["id"], "title": row["title"], "state": row["state"],
                                   "report": row["report"], "steps": json.loads(row["results"])},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def claim(self):
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM jobs WHERE due<=? AND
                (state='queued' OR (state='running' AND lease<?)) ORDER BY due LIMIT 1""",
                             (now, now)).fetchone()
            if row:
                # Interrupted writes/tests are not blindly replayed. Read-only
                # and sandbox steps also stop for inspection after a crash.
                if row["state"] == "running":
                    db.execute("UPDATE jobs SET state='failed', interval=0, lease=0, report=?, notified=0 WHERE id=?",
                               ("Interrupted during a step; inspect results before recreating.", row["id"]))
                    return None
                db.execute("UPDATE jobs SET state='running', lease=? WHERE id=?", (now + 600, row["id"]))
                return dict(row)

    async def run(self, row):
        results = json.loads(row["results"])
        try:
            steps = json.loads(row["steps"])
            for index in range(row["position"], len(steps)):
                with self.db() as db:
                    if db.execute("SELECT state FROM jobs WHERE id=?", (row["id"],)).fetchone()[0] == "cancelled":
                        return
                step = steps[index]
                result = await self.registry.execute(step["tool"], step["arguments"])
                # Keep bounded, useful evidence instead of feeding huge pages
                # back into chat or paying for an extra model summarization.
                results.append({"step": index + 1, "tool": step["tool"],
                                "success": result.success, "message": result.spoken_text[:1500],
                                "data": json.dumps(result.data, default=str)[:12000]})
                with self.db() as db:
                    db.execute("UPDATE jobs SET position=?, results=?, lease=? WHERE id=? AND state='running'",
                               (index + 1, json.dumps(results), time.time() + 600, row["id"]))
                if not result.success:
                    raise ValueError(result.spoken_text[:400])
            state, report = "complete", f"{row['title']}: completed {len(steps)} steps. " + " ".join(r["message"] for r in results)[-1800:]
        except asyncio.CancelledError:
            with self.db() as db:
                db.execute("UPDATE jobs SET state='failed', interval=0, lease=0, report=?, notified=0 WHERE id=? AND state='running'",
                           ("Interrupted by shutdown; not automatically replayed.", row["id"]))
            raise
        except Exception as error:
            state, report = "failed", f"{row['title']}: stopped at step {len(results) or 1}. {str(error)[:400]}"
        with self.db() as db:
            db.execute("UPDATE jobs SET state=?, report=?, notified=0, lease=0 WHERE id=? AND state='running'",
                       (state, report, row["id"]))

    async def report(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE state IN ('complete','failed') AND notified<=0 AND lease<? LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return
            db.execute("UPDATE jobs SET notified=-1, lease=? WHERE id=?", (time.time() + 30, row["id"]))
        delivered = False
        try:
            result = self.notify(f"Task {row['id']}: {row['report']}")
            delivered = await result if inspect.isawaitable(result) else result
        finally:
            with self.db() as db:
                if delivered is False:
                    db.execute("UPDATE jobs SET notified=0 WHERE id=?", (row["id"],))
                else:
                    db.execute("UPDATE jobs SET notified=1 WHERE id=?", (row["id"],))
                    if row["interval"] and row["state"] == "complete":
                        db.execute("UPDATE jobs SET state='queued', position=0, results='[]', due=? WHERE id=? AND state='complete'",
                                   (time.time() + row["interval"], row["id"]))

    async def loop(self):
        while True:
            try:
                row = self.claim()
                if row:
                    await self.run(row)
                await self.report()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Persisted status remains inspectable; do not kill alarms.
            await asyncio.sleep(1)
