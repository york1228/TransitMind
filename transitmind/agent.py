"""Run the model/tool feedback loop without an Agent framework."""

import json
from collections.abc import Sequence

from .models import AgentRun, Message, Model, TraceEvent
from .tools import ToolRegistry


class Agent:
    def __init__(
        self,
        model: Model,
        tools: ToolRegistry,
        max_model_turns: int = 4,
        *,
        system_prompt: str = "Use the provided tools when metadata is needed.",
    ) -> None:
        if type(max_model_turns) is not int or max_model_turns < 1:
            raise ValueError("max_model_turns must be a positive integer.")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("A nonempty system prompt is required.")
        self.model = model
        self.tools = tools
        self.max_model_turns = max_model_turns
        self.system_prompt = system_prompt

    def run(
        self, question: str, *, history: Sequence[Message] = (),
        context_messages: Sequence[Message] = (),
    ) -> AgentRun:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("A nonempty question is required.")

        messages = [
            # 当前规则、会话历史、检索参考、本次问题按顺序组成模型输入。
            Message("system", self.system_prompt),
            *history,
            # 这里的 *history 是展开语法。如果 history 有 6 条消息，就在列表中间放入这 6 条消息。
            *context_messages,
            Message("user", question),
        ]
        events: list[TraceEvent] = []
        seen_call_ids: set[str] = set()
        definitions = self.tools.definitions()

        for turn in range(1, self.max_model_turns + 1):
            events.append(TraceEvent("model_request", turn, {"message_count": len(messages)}))
            response = self.model.complete(tuple(messages), definitions)
            if response.role != "assistant":
                raise ValueError("A model must return an assistant message.")

            call_ids = [call.id for call in response.tool_calls]
            if any(not isinstance(call_id, str) or not call_id for call_id in call_ids):
                raise ValueError("Every tool call needs a nonempty string ID.")
            if len(set(call_ids)) != len(call_ids) or seen_call_ids.intersection(call_ids):
                raise ValueError("Tool call IDs must be unique within a run.")
            seen_call_ids.update(call_ids)

            messages.append(response)
            events.append(
                TraceEvent("model_response", turn, {"tool_call_count": len(response.tool_calls)})
            )

            if not response.tool_calls:
                if not response.content.strip():
                    raise ValueError("An assistant must return tool calls or an answer.")
                events.append(TraceEvent("run_stopped", turn, {"reason": "completed"}))
                return AgentRun(tuple(messages), tuple(events), response.content, "completed")

            for call in response.tool_calls:
                events.append(
                    TraceEvent(
                        "tool_request", turn,
                        {"call_id": call.id, "name": call.name, "arguments": call.arguments},
                    )
                )
                result = self.tools.execute(call)
                # 只回传模型需要的内容，避免把 metadata 中的表格再次发给 DeepSeek。
                model_result = {
                    "success": result.success,
                    "content": result.content,
                    "error_code": result.error_code,
                }
                messages.append(
                    Message("tool", json.dumps(model_result, ensure_ascii=False), tool_call_id=call.id)
                )
                # 结构化数据放进事件，保存 JSON 或接页面时用 call_id 对应工具请求。
                events.append(
                    TraceEvent(
                        "tool_result", turn,
                        {
                            "call_id": call.id, "name": call.name,
                            "success": result.success, "error_code": result.error_code,
                            "metadata": result.metadata,
                        },
                    )
                )

        events.append(
            TraceEvent("run_stopped", self.max_model_turns, {"reason": "model_turn_limit"})
        )
        return AgentRun(tuple(messages), tuple(events), None, "model_turn_limit")

