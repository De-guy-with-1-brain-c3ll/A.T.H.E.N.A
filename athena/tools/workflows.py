"""Model-facing durable task management."""
from athena.tools.models import ToolDefinition, ToolResult
from athena.workflows import ALLOWED


class WorkflowTool:
    definition = ToolDefinition(
        name="background_workflow",
        description="Create multi-step background procedures; return immediately, report completion/failure proactively. Use repeat_seconds only when user explicitly requests recurrence. Steps execute in order and stop on failure. Status gives verified progress. Only read tools and sandboxed coding are supported; downloads, uploads and shell commands require separate approval.",
        parameters={"type": "object", "properties": {
            "action": {"enum": ["create", "status", "cancel", "export_report"]},
            "title": {"type": "string", "minLength": 1, "maxLength": 160},
            "id": {"type": "string", "maxLength": 40},
            "repeat_seconds": {"type": "integer", "minimum": 300, "maximum": 2592000},
            "steps": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                "type": "object", "properties": {"tool": {"type": "string", "enum": sorted(ALLOWED)},
                    "arguments": {"type": "object"}}, "required": ["tool", "arguments"], "additionalProperties": False}}
        }, "required": ["action"], "additionalProperties": False})

    def bind(self, services): self.manager = services.get("workflows")

    async def execute(self, arguments):
        if not self.manager:
            return ToolResult(False, "Background procedures are unavailable.")
        try:
            if arguments["action"] == "create":
                ident = self.manager.create(arguments["title"], arguments["steps"], arguments.get("repeat_seconds", 0))
                return ToolResult(True, "Started in the background. I'll report when it finishes.", {"task_id": ident})
            if arguments["action"] == "cancel":
                ok = self.manager.cancel(arguments["id"])
                return ToolResult(ok, "Task cancelled; an in-flight step may finish." if ok else "Task not found.")
            if arguments["action"] == "export_report":
                import asyncio
                path = await asyncio.to_thread(self.manager.export, arguments["id"])
                return ToolResult(True, "Saved the task report. Ask me to send it to your PC if needed.", {"path": path})
            rows = self.manager.rows(arguments.get("id"))
            return ToolResult(True, "No recorded tasks." if not rows else "; ".join(
                f"{r['title']}: {r['state']}, {r['completed_steps']}/{r['total_steps']} steps" for r in rows), {"tasks": rows})
        except (KeyError, ValueError) as error:
            return ToolResult(False, str(error))


def create_tools(): return [WorkflowTool()]
