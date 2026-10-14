"""A scripted model for the real database SQL demo; no LLM or invented rows."""

import json
from typing import Sequence

from .models import Message, ToolCall, ToolDefinition


class ScriptedSqlModel:
    """Fixed tool choices for learning; answers use the actual database result."""

    def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition],
    ) -> Message:
        # 这是预先写好的模型替身，只演示链路，不理解自然语言，也不调用 DeepSeek。
        if messages[-1].role == "user":
            return Message("assistant", tool_calls=(
                ToolCall("sql-demo-schema", "describe_table", {"table": "agency"}),
            ))

        result = json.loads(messages[-1].content)
        if not result["success"]:
            return Message("assistant", f"The tool failed: {result['error_code']}.")
        if messages[-1].tool_call_id == "sql-demo-schema":
            return Message("assistant", tool_calls=(
                ToolCall("sql-demo-query", "run_sql", {
                    "sql": "SELECT agency_id, agency_name FROM public.agency ORDER BY agency_id",
                }),
            ))

        # 记录来自 run_sql，不写死机构名称；工具结果回到模型后才生成最终回答。
        data = json.loads(result["content"])
        records = "; ".join(f"ID {row[0]}: {row[1]}" for row in data["rows"])
        return Message("assistant", f"Database returned {data['row_count']} agency records: {records}")
