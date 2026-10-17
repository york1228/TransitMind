"""Translate our messages to the DeepSeek API and translate its reply back."""
# 客户端创建
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AuthenticationError, OpenAI

# 保留旧模块的 ModelCallError 导出；核心循环直接依赖公共类型，不依赖 SDK。
from .models import Message, ModelCallError, ToolCall, ToolDefinition


def message_to_api(message: Message) -> dict[str, Any]:
    # 转换成 API 接受的字典格式
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    if message.role == "tool":
        if not message.tool_call_id:
            raise ModelCallError("A tool result must identify its tool call.")
        payload["tool_call_id"] = message.tool_call_id
    return payload


def tool_to_api(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


class DeepSeekModel:
    def __init__(self, client: OpenAI, model: str = "deepseek-flash") -> None:
        self.client = client
        self.model = model

    def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition], *,
        max_tokens: int = 1536, json_output: bool = False,
    ) -> Message:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [message_to_api(message) for message in messages],
            # 工具结果可能包含多行数据，给最终的中文总结留出空间。
            "max_tokens": max_tokens,
            "stream": False,
            # DeepSeek defaults to thinking mode, which needs extra history fields.
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        # 元数据起草只需要一个 JSON 对象；普通 Agent 调用仍沿用原来的默认设置。
        if json_output:
            request["response_format"] = {"type": "json_object"}
        if tools:
            request["tools"] = [tool_to_api(tool) for tool in tools]
            request["tool_choice"] = "auto"

        try:
            response = self.client.chat.completions.create(**request)
        except AuthenticationError:
            raise ModelCallError("DeepSeek authentication failed. Check DEEPSEEK_API_KEY locally.") from None
        except APITimeoutError:
            raise ModelCallError("DeepSeek request timed out. Check the network before retrying.") from None
        except APIConnectionError:
            raise ModelCallError("Cannot connect to DeepSeek. Check the network and base URL.") from None
        except APIStatusError as error:
            raise ModelCallError(
                f"DeepSeek returned HTTP {error.status_code}. Check the model, account balance, and API settings."
            ) from None
        except APIError:
            raise ModelCallError("The DeepSeek SDK could not complete the request.") from None

        # 先提取 API 实际用量：达到输出上限等不可接受的回复也可能已经消耗 Token。
        # 用 API token 的实际统计，不按字符估算；未返回 usage 时保留为 None。
        api_usage = getattr(response, "usage", None)
        usage = None if api_usage is None else {
            "prompt_tokens": api_usage.prompt_tokens,
            "completion_tokens": api_usage.completion_tokens,
            "total_tokens": api_usage.total_tokens,
        }
        if not response.choices:
            raise ModelCallError("DeepSeek returned no response choices.", usage=usage)
        choice = response.choices[0]
        if choice.finish_reason not in {"stop", "tool_calls"}:
            if choice.finish_reason == "length":
                raise ModelCallError(
                    "DeepSeek response hit the output token limit. "
                    "Use a narrower query or let the Agent summarize truncated results.",
                    usage=usage,
                )
            raise ModelCallError(
                f"DeepSeek stopped with finish_reason={choice.finish_reason!r}; "
                "no final answer was accepted.", usage=usage,
            )
        reply = choice.message
        if reply.role != "assistant":
            raise ModelCallError("DeepSeek must return an assistant message.", usage=usage)
        if getattr(reply, "reasoning_content", None):
            raise ModelCallError("Lesson 2 requires non-thinking mode; the API returned thinking content.", usage=usage)

        calls = []
        for call in reply.tool_calls or ():
            if call.type != "function":
                raise ModelCallError("Only function tool calls are supported in this lesson.", usage=usage)
            try:
                arguments = json.loads(call.function.arguments)
            except (json.JSONDecodeError, TypeError):
                raise ModelCallError("DeepSeek returned invalid JSON tool arguments.", usage=usage) from None
            if not isinstance(arguments, dict):
                raise ModelCallError("DeepSeek tool arguments must be a JSON object.", usage=usage)
            calls.append(ToolCall(call.id, call.function.name, arguments))

        return Message("assistant", reply.content or "", tool_calls=tuple(calls), usage=usage)


def build_deepseek_model() -> DeepSeekModel:
    # Read only this project's file, not a parent project's credentials.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Set DEEPSEEK_API_KEY in the local .env file. Do not send it in chat.")

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    try:
        url = urlsplit(base_url)
        port = url.port
    except ValueError:
        raise ValueError("DEEPSEEK_BASE_URL must be a valid official HTTPS URL.") from None
    if (
        url.scheme != "https"
        or url.hostname != "api.deepseek.com"
        or url.username is not None
        or url.password is not None
        or port not in (None, 443)
        or url.path not in ("", "/v1")
        or url.query
        or url.fragment
    ):
        raise ValueError("Lesson 2 uses the official HTTPS DeepSeek endpoint only.")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
    if not model:
        raise ValueError("DEEPSEEK_MODEL must not be empty.")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0, max_retries=0)
    # 创建一个 SDK 客户端，告诉它用什么 Key、请求哪个地址、设置多长的超时，以及是否自动重试。
    return DeepSeekModel(client, model)
