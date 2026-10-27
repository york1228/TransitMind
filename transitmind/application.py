"""Run one query for CLI or HTTP callers, including optional memory and cleanup."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from time import perf_counter
from typing import Any

from .agent import DEFAULT_MAX_SQL_ATTEMPTS, DEFAULT_MAX_TOOL_CALLS
from .database_config import EXPECTED_DATABASE
from .models import AgentRun, Message, TraceEvent
from .runtime import CONVERSATION_SYSTEM_PROMPT, EXPERIENCE_SYSTEM_PROMPT, build_agent


@dataclass(frozen=True)
class QueryExecution:
    result: AgentRun
    payload: dict[str, Any]
    conversation_saved_path: Path | None = None

# 复用已有的历史读取、Agent 调用和成功保存流程
def execute_query(
    question: str, *, live: bool = False, database: bool = False, sql: bool = False,
    table: str | None = None, max_model_turns: int = 4, schema_memory: bool = False,
    schema_retrieval: str = "keyword", schema_expand: bool = False,
    conversation_id: str | None = None, history_turns: int = 3, experience_memory: bool = False,
    on_prepared: Callable[[dict[str, Any], str], None] | None = None,
    on_event: Callable[[TraceEvent], None] | None = None,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_sql_attempts: int = DEFAULT_MAX_SQL_ATTEMPTS,
) -> QueryExecution:
    """Prepare context, run a fresh Agent and return the shared trace fields."""
    started = perf_counter()
    history: tuple[Message, ...] = ()
    context_messages: tuple[Message, ...] = ()
    conversation = None
    store = None
    saved_path = None
    context_metadata: dict[str, Any] = {}

    # 每次调用都有自己的 Agent 和资源作用域，不跨网页请求共享有状态的实例。
    # ExitStack：正常结束或异常离开时，都释放本次模型客户端占用的连接资源。
    with ExitStack() as resources:
        # runtime 继续组装模型、工具和提示词；本层只负责一次查询的运行流程。
        agent, mode, detail = build_agent(
            live=live, database=database, sql=sql, table=table,
            max_model_turns=max_model_turns, schema_memory=schema_memory,
            schema_retrieval=schema_retrieval, schema_expand=schema_expand,
            resources=resources,
            max_tool_calls=max_tool_calls, max_sql_attempts=max_sql_attempts,
        )

        if conversation_id is not None:
            from .conversation_store import JsonConversationStore, select_history

            store = JsonConversationStore()
            conversation = store.load(conversation_id, mode)
            # conversation：根据会话 ID，取得这段会话保存的全部历史。
            history = select_history(conversation.messages, history_turns)
            # history：从全部历史中选出这次要交给模型的部分，默认最近 3 次完整提问。
            agent.system_prompt += " " + CONVERSATION_SYSTEM_PROMPT
            context_metadata.update({
                "conversation_id": conversation.id, "history_message_count": len(history),
                "history_turn_limit": history_turns, "conversation_saved": False,
            })

        # 开启经验记忆时，在本次调用 Agent 前检索；网页默认不开启此能力。
        if experience_memory:
            from .experience_memory import JsonExperienceStore, build_experience_context

            # 检索在 Python 中完成，不额外调用模型；同一经验可以被不同会话复用。
            experience_matches = JsonExperienceStore().search(question, EXPECTED_DATABASE)
            # 把候选经验送给模型参考。
            context_messages = build_experience_context(experience_matches)
            agent.system_prompt += " " + EXPERIENCE_SYSTEM_PROMPT
            context_metadata["experience_memory"] = {
                "database_id": EXPECTED_DATABASE, "query": question, "matches": experience_matches,
            }

        if schema_memory:
            from .schema_store import DEFAULT_FILE

            # 命中的表和实际使用的版本在 schema_retrieve 工具事件中，不只记录开关。
            context_metadata["schema_memory"] = {
                "enabled": True, "store_path": str(DEFAULT_FILE), "retrieval_mode": schema_retrieval,
                "expansion_enabled": schema_expand,
            }

        if on_prepared is not None:
            # CLI 可在模型请求前打印概要；应用层自身不输出终端或 HTTP 内容。
            on_prepared({"mode": mode, "question": question, **context_metadata}, detail)

        # 事件在发生时交给网页；完整结果仍在运行结束后统一封装。
        result = agent.run(question, history=history, context_messages=context_messages, on_event=on_event)
        if store is not None and conversation is not None and result.stop_reason == "completed":
            # 跳过 system、历史和本次参考经验，只把真正的新问答写进会话。
            new_messages = result.messages[1 + len(history) + len(context_messages):]
            conversation = replace(conversation, messages=conversation.messages + new_messages)
            saved_path = store.save(conversation)
            context_metadata["conversation_saved"] = saved_path is not None

        # 公共记录在 Agent 返回后封装，包含模型错误停止；保留具体 stop_reason 和已有事件。
        # 文件路径、终端输出、HTTP 状态和异常记录仍由调用方处理。
        # 每种返回的停止路径都由循环统一记录实际次数，CLI 和网页共用统计口径。
        stopped = result.events[-1].details
        model_outcomes = [
            event.details for event in result.events if event.kind in {"model_response", "model_error"}
        ]
        recorded_usage = [item["usage"] for item in model_outcomes if item.get("usage") is not None]
        # 第一句选出本次模型请求的正常或失败结果；第二句再选出有 Token 数据的结果。

        token_usage = None if not recorded_usage else {
            key: sum(item[key] for item in recorded_usage)
             # 后面的 sum() 分别累加三个 Token 字段。
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        if token_usage is not None:
            token_usage.update({"recorded_calls": len(recorded_usage), "model_calls": len(model_outcomes)})
        payload = {
            "mode": mode, "question": question, **asdict(result), **context_metadata,
            "token_usage": token_usage,
            "model_duration_ms": round(sum(item["duration_ms"] for item in model_outcomes), 3),
            "tool_duration_ms": round(sum(
                event.details["duration_ms"] for event in result.events if event.kind == "tool_result"
            ), 3),
            "execution_budget": {
                "max_tool_calls": max_tool_calls, "max_sql_attempts": max_sql_attempts,
                "tool_call_count": stopped["tool_call_count"],
                "sql_attempt_count": stopped["sql_attempt_count"],
            },
        }
    # 计入上下文准备、Agent 运行、可选会话保存和客户端释放；不包含 HTTP 传输或页面渲染。
    payload["duration_ms"] = round((perf_counter() - started) * 1000, 3)
    return QueryExecution(result, payload, saved_path)
