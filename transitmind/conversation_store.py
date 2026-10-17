"""Store local conversations as JSON and select recent complete turns."""

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .models import Conversation, Message, ToolCall


DEFAULT_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "conversations"


def select_history(messages: Sequence[Message], turn_limit: int = 3) -> tuple[Message, ...]:
    # 一次用户提问可能产生多条 assistant/tool 消息，按 user 边界保留整次提问。
    # 这样不会只留下工具结果，却把对应的 tool_call 裁掉。
    starts = [index for index, message in enumerate(messages) if message.role == "user"]
    if turn_limit <= 0 or not starts:
        return ()
    start = starts[max(0, len(starts) - turn_limit)]
    return tuple(messages[start:])


class JsonConversationStore:
    def __init__(self, directory: Path = DEFAULT_DIRECTORY) -> None:
        self.directory = directory

    def _path(self, conversation_id: str) -> Path:
        # ID 会成为文件名，限定为简单标识，避免把输入当成任意文件路径。
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", conversation_id):
            raise ValueError("Conversation ID must use 1 to 64 letters, numbers, underscores or hyphens.")
        return self.directory / f"{conversation_id}.json"

    def load(self, conversation_id: str, mode: str) -> Conversation:
        path = self._path(conversation_id)
        if not path.exists():
            now = datetime.now(timezone.utc).isoformat()
            return Conversation(conversation_id, mode, now, now)

        data = json.loads(path.read_text(encoding="utf-8"))
        # 文件存在时，读取 JSON，把字典重新构造成 Message 和 ToolCall，最后返回 Conversation。
        if data["mode"] != mode:
            # 防止教学字典会话与真实数据库会话混用。更换运行模式时使用新 ID。
            raise ValueError("This conversation belongs to a different run mode. Use a new conversation ID.")
        # JSON 读回的是字典和列表，重建 Message/ToolCall 后才能送入现有模型适配器。
        messages = tuple(
            Message(
                role=item["role"],
                content=item["content"],
                tool_calls=tuple(ToolCall(**call) for call in item.get("tool_calls", ())),
                tool_call_id=item.get("tool_call_id"),
                usage=item.get("usage"),
            )
            for item in data["messages"]
        )
        return Conversation(
            id=data["id"], mode=data["mode"],
            created_at=data["created_at"], updated_at=data["updated_at"], messages=messages,
        )

    def save(self, conversation: Conversation) -> Path:
        path = self._path(conversation.id)
        self.directory.mkdir(parents=True, exist_ok=True)
        conversation = replace(conversation, updated_at=datetime.now(timezone.utc).isoformat())
        # 保存全部成功提问的消息；发给模型的历史窗口由 select_history 单独选择。
        payload = json.dumps(asdict(conversation), ensure_ascii=False, indent=2)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        # 先写临时文件再替换，保存中断时仍保留上一份完整会话。
        temporary.replace(path)
        return path
