"""The messages exchanged between a model, the agent, and its tools."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Sequence


class ModelCallError(RuntimeError):
    """A safe model diagnostic, with API usage when a response was received."""

    def __init__(self, message: str, *, usage: dict[str, int] | None = None) -> None:
        super().__init__(message)
        # API 返回内容不可接受时，仍可保留它实际返回的用量；没有用量就保持 None。
        self.usage = usage


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    # 模型 API 返回的用量，只供运行统计；适配器不会把它发回模型。
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class Conversation:
    # 一个会话保存多次提问的消息；mode 标记使用的模型/工具运行模式。
    id: str
    mode: str
    created_at: str
    updated_at: str
    messages: tuple[Message, ...] = ()


@dataclass(frozen=True)
class QueryExperience:
    # 经验记录保存人工认可的查询方法和业务口径，实际查询结果仍从数据库获取。
    id: str
    database_id: str
    question: str
    notes: str
    keywords: tuple[str, ...]
    sql: str = ""
    approved_at: str = ""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    success: bool
    # content 交给模型；metadata 留给运行记录和页面，不加入模型上下文。
    content: str
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceEvent:
    kind: str
    model_turn: int
    details: dict[str, Any] = field(default_factory=dict)


StopReason = Literal[
    "completed", "model_turn_limit", "tool_call_limit", "sql_attempt_limit", "sql_recovery_stopped",
    "repeated_failed_sql", "error",
]


@dataclass(frozen=True)
class AgentRun:
    messages: tuple[Message, ...]
    events: tuple[TraceEvent, ...]
    final_answer: str | None
    stop_reason: StopReason
    # 模型请求失败时保留清理后的诊断，已有消息和事件仍随运行结果返回。
    error: str | None = None


class Model(Protocol):
    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> Message:
        """Return an assistant message, optionally requesting tool calls."""
        ...

