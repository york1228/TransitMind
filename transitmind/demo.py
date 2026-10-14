"""Scripted model responses and deliberately small teaching schema fixtures."""

import json
from typing import Any, Sequence

from .models import Message, ToolCall, ToolDefinition, ToolResult
from .tools import Tool, ToolRegistry


TEACHING_SCHEMAS = {
    "routes": {
        "columns": ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
        "foreign_keys": {"agency_id": "agency.agency_id"},
    },
    "trips": {
        "columns": ["trip_id", "route_id", "service_id", "trip_headsign", "direction_id"],
        "foreign_keys": {"route_id": "routes.route_id"},
    },
    "stops": {
        "columns": ["stop_id", "stop_name", "stop_lat", "stop_lon", "parent_station"],
        "foreign_keys": {"parent_station": "stops.stop_id"},
    },
}


def describe_table(arguments: dict[str, Any]) -> ToolResult:
    if set(arguments) != {"table"} or not isinstance(arguments.get("table"), str):
        return ToolResult(False, "Provide exactly one string argument named table.", "invalid_arguments")
    table = arguments["table"]
    schema = TEACHING_SCHEMAS.get(table)
    if schema is None:
        return ToolResult(False, "The table is not in the teaching fixture.", "unknown_table")
    return ToolResult(
        True,
        json.dumps({"source": "lesson_fixture", "table": table, **schema}),
    )


def build_demo_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="describe_table",
                description="Describe a GTFS teaching table. Does not access a database.",
                parameters={
                    "type": "object",
                    "properties": {"table": {"type": "string"}},
                    "required": ["table"],
                    "additionalProperties": False,
                },
            ),
            handler=describe_table,
        )
    )
    return registry


class ScriptedModel:
    """A deterministic stand-in; it does not understand natural-language questions."""

    def __init__(self, table: str = "trips") -> None:
        self.table = table

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> Message:
        if messages[-1].role == "user":
            return Message(
                "assistant",
                tool_calls=(ToolCall("demo-call-1", "describe_table", {"table": self.table}),),
            )

        result = json.loads(messages[-1].content)
        if not result["success"]:
            return Message("assistant", f"The tool failed: {result['error_code']}.")
        schema = json.loads(result["content"])
        columns = ", ".join(schema["columns"])
        # 脚本模型没有变成真实 LLM；只根据工具来源区分教学字段与真实数据库字段。
        fields = "database fields" if schema["source"] == "postgresql" else "teaching fields"
        return Message("assistant", f"{schema['table']} has these {fields}: {columns}.")

