from __future__ import annotations

from typing import Any

from athena.settings.store import RuntimeSettingsStore
from athena.tools.models import ToolDefinition, ToolResult


class SettingsTool:
    definition = ToolDefinition(
        name="manage_settings",
        description=(
            "List, inspect, change, or reset an approved ATHENA setting. Use this "
            "when Benjamin asks to adjust sensitivity, timing, voice, response "
            "length or creativity, language, or memory. Never use it for secrets."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "get", "set", "reset"]},
                "setting": {"type": "string"},
                "value": {},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    )

    def __init__(self) -> None:
        self.store: RuntimeSettingsStore | None = None

    def bind(self, services: dict[str, Any]) -> None:
        store = services.get("settings")
        if isinstance(store, RuntimeSettingsStore):
            self.store = store

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if self.store is None:
            return ToolResult(False, "The settings service is unavailable.")
        action = arguments["action"]
        name = arguments.get("setting")
        if action == "list":
            settings = self.store.public_settings()
            return ToolResult(True, "I listed the available settings.", settings)
        if not name:
            return ToolResult(False, "A setting name is required.")
        if action == "get":
            details = self.store.public_settings().get(name)
            if details is None:
                return ToolResult(False, f"{name} is not an approved setting.")
            return ToolResult(True, f"{name} is {details['value']}.", details)
        if action == "set":
            if "value" not in arguments:
                return ToolResult(False, "A new value is required.")
            value, live = self.store.set(name, arguments["value"])
        elif action == "reset":
            value, live = self.store.reset(name)
        else:
            return ToolResult(False, f"Unknown settings action: {action}")
        suffix = "It applies on the next turn." if live else "Restart ATHENA to apply it."
        return ToolResult(
            True,
            f"I set {name} to {value}. {suffix}",
            {"setting": name, "value": value, "applies_live": live},
        )


TOOL = SettingsTool()
