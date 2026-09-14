from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class PermissionLevel(Enum):
    SAFE = "safe"
    CONFIRM = "confirm"
    DANGEROUS = "dangerous"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    permission: PermissionLevel = PermissionLevel.SAFE
    timeout_seconds: float = 10.0
    cancellable: bool = True

    def for_model(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    success: bool
    spoken_text: str
    data: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    definition: ToolDefinition

    async def execute(self, arguments: dict[str, Any]) -> ToolResult: ...
