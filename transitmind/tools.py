"""Resolve tool names and return a consistent execution result."""

from dataclasses import dataclass
from typing import Any, Callable

from .models import ToolCall, ToolDefinition, ToolResult


@dataclass(frozen=True)
class Tool:
    definition: ToolDefinition
    handler: Callable[[dict[str, Any]], ToolResult]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if not name:
            raise ValueError("A tool must have a name.")
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = tool

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(False, "The requested tool is not registered.", "unknown_tool")
        if not isinstance(call.arguments, dict):
            return ToolResult(False, "Tool arguments must be an object.", "invalid_arguments")

        # Each teaching tool validates its own arguments; JSON Schema is a model hint.
        try:
            result = tool.handler(call.arguments)
            if not isinstance(result, ToolResult):
                raise TypeError("A tool handler must return ToolResult.")
            return result
        except Exception:
            return ToolResult(False, "The tool handler failed.", "tool_execution_error")

