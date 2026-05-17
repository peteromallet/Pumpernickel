"""DeepSeek chat client adapted to the Anthropic-shaped agent loop."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Sequence

import httpx

from app.config import get_settings


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class DeepSeekClient:
    """Expose ``messages.create`` so the existing turn loop can stay provider-neutral."""

    def __init__(self) -> None:
        self.messages = DeepSeekMessages()


class DeepSeekMessages:
    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: Sequence[dict[str, Any]],
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> Any:
        settings = get_settings()
        if settings.deepseek_api_key is None:
            raise ValueError("DEEPSEEK_API_KEY not found in environment")

        payload: dict[str, Any] = {
            "model": model,
            "messages": _to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = [_to_openai_tool(tool) for tool in tools]
        if settings.deepseek_reasoning_effort:
            payload["reasoning_effort"] = settings.deepseek_reasoning_effort
        if settings.deepseek_thinking_enabled:
            payload["thinking"] = {"type": "enabled"}

        async with httpx.AsyncClient(
            timeout=settings.provider_call_timeout_seconds
        ) as client:
            response = await client.post(
                f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": (
                        "Bearer "
                        f"{settings.deepseek_api_key.get_secret_value()}"
                    ),
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
        return _to_anthropic_like_response(response.json())


def _to_openai_messages(
    system: Sequence[dict[str, Any]],
    messages: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": _system_text(system)}]
    for message in messages or []:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "assistant" and isinstance(content, list):
            out.append(_assistant_message_from_blocks(content))
        elif role == "user" and isinstance(content, list):
            user_text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id") or ""),
                            "content": str(block.get("content") or ""),
                        }
                    )
                elif block.get("type") == "text":
                    user_text_parts.append(str(block.get("text") or ""))
            if user_text_parts:
                out.append({"role": "user", "content": "\n\n".join(user_text_parts)})
        else:
            out.append({"role": role, "content": _content_to_text(content)})
    return out


def _system_text(system: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in system or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n\n".join(part for part in parts if part)


def _assistant_message_from_blocks(blocks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning_content: str | None = None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "openai_assistant_message" and isinstance(
            block.get("message"), dict
        ):
            raw = dict(block["message"])
            raw["role"] = "assistant"
            return raw
        if block_type == "text" and block.get("text"):
            text_parts.append(str(block["text"]))
        elif block_type == "reasoning_content" and block.get("reasoning_content"):
            reasoning_content = str(block["reasoning_content"])
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    row: dict[str, Any] = {
        "role": "assistant",
        "content": "\n\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        row["tool_calls"] = tool_calls
    if reasoning_content:
        row["reasoning_content"] = reasoning_content
    return row


def _to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.get("name"),
            "description": tool.get("description") or "",
            "parameters": tool.get("input_schema")
            or {"type": "object", "properties": {}},
        },
    }


def _to_anthropic_like_response(response: dict[str, Any]) -> Any:
    choices = response.get("choices") or []
    message = choices[0].get("message") if choices else {}
    content_blocks: list[Any] = []
    if message:
        content_blocks.append(
            SimpleNamespace(type="openai_assistant_message", message=message)
        )
    reasoning_content = message.get("reasoning_content") if message else None
    if reasoning_content:
        content_blocks.append(
            SimpleNamespace(
                type="reasoning_content", reasoning_content=str(reasoning_content)
            )
        )
    if message.get("content"):
        content_blocks.append(SimpleNamespace(type="text", text=message["content"]))
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed_args = {}
        content_blocks.append(
            SimpleNamespace(
                type="tool_use",
                id=tool_call.get("id"),
                name=function.get("name"),
                input=parsed_args,
            )
        )

    usage = response.get("usage") or {}
    normalized_usage = SimpleNamespace(
        input_tokens=usage.get("prompt_tokens", 0) or 0,
        output_tokens=usage.get("completion_tokens", 0) or 0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    if message.get("tool_calls"):
        stop_reason = "tool_use"
    elif choices:
        stop_reason = choices[0].get("finish_reason")
    else:
        stop_reason = None
    return SimpleNamespace(
        content=content_blocks,
        usage=normalized_usage,
        stop_reason=stop_reason,
        raw_response=response,
    )


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or block))
            else:
                parts.append(str(block))
        return "\n\n".join(parts)
    return str(content or "")
