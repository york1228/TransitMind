"""Run the model/tool feedback loop without an Agent framework."""

import json
from collections.abc import Callable, Sequence
from time import perf_counter

from .models import AgentRun, Message, Model, ModelCallError, StopReason, ToolCall, TraceEvent
from .sql_recovery import decide_sql_recovery
from .tools import ToolRegistry


# 预算属于一次 Agent 运行；CLI、网页和应用层共用这里的默认值。
DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_MAX_SQL_ATTEMPTS = 3


class Agent:
    def __init__(
        self,
        model: Model,
        tools: ToolRegistry,
        max_model_turns: int = 4,
        *,
        system_prompt: str = "Use the provided tools when metadata is needed.",
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_sql_attempts: int = DEFAULT_MAX_SQL_ATTEMPTS,
    ) -> None:
        if type(max_model_turns) is not int or max_model_turns < 1:
            raise ValueError("max_model_turns must be a positive integer.")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("A nonempty system prompt is required.")
        for name, value in (("max_tool_calls", max_tool_calls), ("max_sql_attempts", max_sql_attempts)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer.")
        self.model = model
        self.tools = tools
        self.max_model_turns = max_model_turns
        self.system_prompt = system_prompt
        self.max_tool_calls = max_tool_calls
        self.max_sql_attempts = max_sql_attempts

    def run(
        self, question: str, *, history: Sequence[Message] = (),
        context_messages: Sequence[Message] = (),
        on_event: Callable[[TraceEvent], None] | None = None,
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
        # 在 run 内计数，每次提问从零开始；同一轮返回多个工具时逐个检查。
        tool_call_count = 0
        sql_attempt_count = 0
        # 只保存本次运行中允许修正的 SQL 失败；不写入会话或跨提问共享。
        failed_sql_calls: dict[str, dict[str, object]] = {}

        def emit(kind: str, turn: int, details: dict[str, object]) -> None:
            # 同一条事件先留在 trace，再通知调用方；CLI 不传回调时沿用原行为。
            event = TraceEvent(kind, turn, details)
            events.append(event)
            if on_event is not None:
                on_event(event)

        def finish(
            reason: StopReason, turn: int, final_answer: str | None = None,
            blocked_call: ToolCall | None = None,
            *, failure: dict[str, object] | None = None, error: str | None = None,
        ) -> AgentRun:
            details = {
                "reason": reason,
                "tool_call_count": tool_call_count, "max_tool_calls": self.max_tool_calls,
                "sql_attempt_count": sql_attempt_count, "max_sql_attempts": self.max_sql_attempts,
            }
            if blocked_call is not None:
                # 被拦截的请求单独记录，不伪造 tool_result，也不计为已经执行。
                details["blocked_call"] = {
                    "call_id": blocked_call.id, "name": blocked_call.name,
                    "arguments": blocked_call.arguments,
                }
            if failure is not None:
                # 这个请求已经执行且失败，与预算检查时尚未执行的 blocked_call 区分。
                details["failure"] = failure
            emit("run_stopped", turn, details)
            return AgentRun(tuple(messages), tuple(events), final_answer, reason, error=error)

        # 在模型请求、工具执行前后计时，把耗时写进对应事件
        for turn in range(1, self.max_model_turns + 1):
#             duration_ms          → 总耗时
#             model_duration_ms    → 模型请求累计耗时
#             tool_duration_ms     → 工具执行累计耗时
#             token_usage          → 输入、输出、合计 Token
            emit("model_request", turn, {"message_count": len(messages)})
            # 开始前记一次时间，结束后再取时间，两者相减并转成毫秒。工具执行也是相同做法。
            model_started = perf_counter()
            try:
                response = self.model.complete(tuple(messages), definitions)
            except ModelCallError as error:
                # 适配器已清理诊断；保留已有 SQL、工具结果和本次失败耗时，不接受残缺回答。
                emit("model_error", turn, {
                    "error": str(error), "usage": error.usage,
                    "duration_ms": round((perf_counter() - model_started) * 1000, 3),
                })
                return finish("error", turn, error=str(error))
            model_duration_ms = round((perf_counter() - model_started) * 1000, 3)

            if response.role != "assistant":
                raise ValueError("A model must return an assistant message.")

            call_ids = [call.id for call in response.tool_calls]
            if any(not isinstance(call_id, str) or not call_id for call_id in call_ids):
                raise ValueError("Every tool call needs a nonempty string ID.")
            if len(set(call_ids)) != len(call_ids) or seen_call_ids.intersection(call_ids):
                raise ValueError("Tool call IDs must be unique within a run.")
            seen_call_ids.update(call_ids)

            messages.append(response)
            emit("model_response", turn, {
                "tool_call_count": len(response.tool_calls),
                "duration_ms": model_duration_ms, "usage": response.usage,
            })

            if not response.tool_calls:
                if not response.content.strip():
                    raise ValueError("An assistant must return tool calls or an answer.")
                return finish("completed", turn, response.content)

            for call in response.tool_calls:
                # 先检查再执行；SQL 同时占用工具总额度和 SQL 尝试额度。
                # 恰好用完额度并不立刻结束，下一轮仍可直接给出最终回答。
                if tool_call_count >= self.max_tool_calls:
                    return finish("tool_call_limit", turn, blocked_call=call)
                if call.name == "run_sql" and sql_attempt_count >= self.max_sql_attempts:
                    return finish("sql_attempt_limit", turn, blocked_call=call)
                # 参数按键排序后比较，忽略字典键顺序和调用 ID；SQL 字符串本身不改写。
                sql_key = (
                    json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
                    if call.name == "run_sql" else None
                )
                if sql_key is not None and sql_key in failed_sql_calls:
                    # 这组参数在本次运行中已失败；原样重发不执行、不扣次数，也不伪造结果。
                    return finish(
                        "repeated_failed_sql", turn, blocked_call=call,
                        failure=failed_sql_calls[sql_key],
                    )
                emit("tool_request", turn, {
                    "call_id": call.id, "name": call.name, "arguments": call.arguments,
                })
                # 进入注册表就算一次尝试，参数错误、SQL 拒绝和执行失败均不退回额度。
                tool_call_count += 1
                if call.name == "run_sql":
                    sql_attempt_count += 1
                started = perf_counter()
                result = self.tools.execute(call)
                duration_ms = round((perf_counter() - started) * 1000, 3)
                # 仅处理 run_sql 的失败；其他工具仍按原来的结果回传流程运行。
                recovery = None
                if call.name == "run_sql" and not result.success:
                    recovery = decide_sql_recovery(result.error_code)
                # 只回传模型需要的内容，避免把 metadata 中的表格再次发给 DeepSeek。
                model_result = {
                    "success": result.success,
                    "content": result.content,
                    "error_code": result.error_code,
                }
                if recovery is not None:
                    # 处理决定和诊断一起交给模型，下一轮可查结构或提交修正后的 SQL。
                    model_result["recovery"] = recovery
                messages.append(
                    Message("tool", json.dumps(model_result, ensure_ascii=False), tool_call_id=call.id)
                )
                # 结构化数据放进事件，保存 JSON 或接页面时用 call_id 对应工具请求。
                emit("tool_result", turn, {
                    "call_id": call.id, "name": call.name,
                    "success": result.success, "error_code": result.error_code,
                    "duration_ms": duration_ms,
                    "metadata": result.metadata,
                    **({"recovery": recovery} if recovery is not None else {}),
                })
                if recovery is not None:
                    failure: dict[str, object] = {
                        "call_id": call.id, "name": call.name, "error_code": result.error_code,
                        "recovery": recovery,
                    }
                    if recovery["action"] == "stop":
                        # 不可恢复错误由程序结束，不再消耗模型请求或执行本批剩余工具。
                        return finish("sql_recovery_stopped", turn, failure=failure)
                    if sql_key is not None:
                        # 查字段等其他工具不会清空记录；下一次 run_sql 必须提交修改后的参数。
                        failed_sql_calls[sql_key] = failure

        return finish("model_turn_limit", self.max_model_turns)

