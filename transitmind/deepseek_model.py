"""Translate our messages to the DeepSeek API and translate its reply back."""
# 客户端创建
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from httpx import TimeoutException, TransportError
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AuthenticationError, OpenAI
from openai.types.chat import ChatCompletionMessage

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


def token_usage(api_usage: Any) -> dict[str, int] | None:
    # 普通回复和流式末包共用同一份 API 用量转换；不累计每个分片。
    return None if api_usage is None else {
        "prompt_tokens": api_usage.prompt_tokens,
        "completion_tokens": api_usage.completion_tokens,
        "total_tokens": api_usage.total_tokens,
    }


class DeepSeekModel:
    def __init__(self, client: OpenAI, model: str = "deepseek-flash") -> None:
        self.client = client
        self.model = model

    def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition], *,
        max_tokens: int = 1536, json_output: bool = False,
        on_text: Callable[[str], None] | None = None,
        # 评测裁判使用低温度减少判定波动；普通 Agent 调用不传，保持原请求。
        temperature: float | None = None,
        # 怎样接收文字分片；怎样把工具参数拼完整；何时才接受整轮回复
    ) -> Message:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [message_to_api(message) for message in messages],
            # 工具结果可能包含多行数据，给最终的中文总结留出空间。
            "max_tokens": max_tokens,
            "stream": on_text is not None,
            # DeepSeek defaults to thinking mode, which needs extra history fields.
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if temperature is not None:
            request["temperature"] = temperature
        # 元数据起草只需要一个 JSON 对象；普通 Agent 调用仍沿用原来的默认设置。
        if json_output:
            request["response_format"] = {"type": "json_object"}
        if tools:
            request["tools"] = [tool_to_api(tool) for tool in tools]
            request["tool_choice"] = "auto"
        if on_text is not None:
            request["stream_options"] = {"include_usage": True}

        usage = None
        try:
            if on_text is None:
                response = self.client.chat.completions.create(**request)
                usage = token_usage(getattr(response, "usage", None))
                if not response.choices:
                    raise ModelCallError("DeepSeek returned no response choices.", usage=usage)
                choice = response.choices[0]
                finish_reason = choice.finish_reason
                reply = choice.message
            else:
                parts: list[str] = []
                builders: dict[int, dict[str, Any]] = {}
                finish_reason = None
                # 流的连接在正常结束和异常时都关闭；客户端仍由应用层释放。
                with self.client.chat.completions.create(**request) as stream:
                    for chunk in stream:
                        if chunk.usage is not None:
                            usage = token_usage(chunk.usage)
                        if not chunk.choices:
                            continue
                        choice = chunk.choices[0]
                        delta = choice.delta
                        if delta.role is not None and delta.role != "assistant":
                            raise ModelCallError("DeepSeek must return an assistant message.", usage=usage)
                        if getattr(delta, "reasoning_content", None):
                            raise ModelCallError("Lesson 2 requires non-thinking mode; the API returned thinking content.", usage=usage)
                        if delta.content:
                            parts.append(delta.content)
                            on_text(delta.content)
                        # 同一调用的 JSON 参数可能分在多个包，按 index 拼完整后才解析。
                        for call in delta.tool_calls or ():
                            if call.type is not None and call.type != "function":
                                raise ModelCallError("Only function tool calls are supported in this lesson.", usage=usage)
                            builder = builders.setdefault(call.index, {
                                "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                            })
                            if call.id:
                                builder["id"] = call.id
                            if call.function is not None:
                                builder["function"]["name"] += call.function.name or ""
                                builder["function"]["arguments"] += call.function.arguments or ""
                        if choice.finish_reason is not None:
                            finish_reason = choice.finish_reason
                reply = ChatCompletionMessage(
                    role="assistant", content="".join(parts),
                    tool_calls=[builders[index] for index in sorted(builders)] or None,
                )
        except AuthenticationError:
            raise ModelCallError("DeepSeek authentication failed. Check DEEPSEEK_API_KEY locally.", usage=usage) from None
        # 流中途的超时或断线可能由 HTTPX 直接抛出，沿用原诊断，不自动重试。
        except (APITimeoutError, TimeoutException):
            raise ModelCallError("DeepSeek request timed out. Check the network before retrying.", usage=usage) from None
        except (APIConnectionError, TransportError):
            raise ModelCallError("Cannot connect to DeepSeek. Check the network and base URL.", usage=usage) from None
        except APIStatusError as error:
            raise ModelCallError(
                f"DeepSeek returned HTTP {error.status_code}. Check the model, account balance, and API settings.",
                usage=usage,
            ) from None
        except APIError:
            raise ModelCallError("The DeepSeek SDK could not complete the request.", usage=usage) from None

        # 先提取 API 实际用量：达到输出上限等不可接受的回复也可能已经消耗 Token。
        # 用 API token 的实际统计，不按字符估算；未返回 usage 时保留为 None。
        if finish_reason not in {"stop", "tool_calls"}:
            if finish_reason == "length":
                raise ModelCallError(
                    "DeepSeek response hit the output token limit. "
                    "Use a narrower query or let the Agent summarize truncated results.",
                    usage=usage,
                )
            raise ModelCallError(
                f"DeepSeek stopped with finish_reason={finish_reason!r}; "
                "no final answer was accepted.", usage=usage,
            )
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

    def complete_stream(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition], *,
        on_text: Callable[[str], None],
    ) -> Message:
        # 可选的流式入口复用 complete 的请求和校验；原 CLI、AI 起草不改变返回方式。
        return self.complete(messages, tools, on_text=on_text)


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
