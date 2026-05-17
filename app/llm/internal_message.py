"""Canonical InternalMessage IR (Project C, C1).

A versioned, provider-neutral message schema with round-trip converters for
Anthropic and DeepSeek-shaped responses/requests.  This module is **opt-in
only** — A2's tested fallback path in ``app/services/agentic.py``
(``_create_message_with_retry`` + ``_anthropic_safe_messages``) is the
default sanitization boundary and is NOT rewired here.  See
``provider_use_canonical_ir`` in ``app/config.py`` and the marker comment
above ``_create_message_with_retry``.

Why
---
Settled Decisions originally argued against a canonical IR (SD-003) because
it duplicated effort without a third provider.  SD-009 reverses this on
user override: the IR ships now so that when a third provider lands the
abstraction is already in place, validated by round-trip tests, and
guarded behind a feature flag so the existing sanitize-at-boundary path
keeps working unchanged.

Shape
-----
* ``InternalMessage`` carries a role + a list of typed content blocks plus a
  ``version`` integer at the top level so future schema migrations can be
  detected at read time.
* ``ContentBlock`` is a discriminated union of ``TextBlock``,
  ``ToolUseBlock``, and ``ToolResultBlock``.  Provider-specific extras
  (DeepSeek's ``openai_assistant_message`` and ``reasoning_content``) are
  intentionally NOT modelled — converters drop them on import.  When a real
  third provider lands the union grows.
* Converters are pure functions: response objects (Anthropic SDK message OR
  the DeepSeek-shaped ``SimpleNamespace`` from
  ``app.services.deepseek._to_anthropic_like_response``) map to a single
  ``InternalMessage`` (the assistant turn).  Request converters take a list
  of canonical messages and emit the provider-native shape consumed by
  ``client.messages.create``.

Integration
-----------
Not wired into the live turn loop.  ``provider_use_canonical_ir`` defaults
to False.  Use the converters in tests or behind the flag when a third
provider is being added.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Union

from pydantic import BaseModel, Field


CURRENT_VERSION: int = 1


# ── content blocks ───────────────────────────────────────────────────────────


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str = ""


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str = ""
    is_error: bool = False


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock]


# ── canonical message ────────────────────────────────────────────────────────


class InternalMessage(BaseModel):
    """Provider-neutral conversation message.

    ``version`` is stamped at the top level (NOT per-block) so schema
    migrations only require a single read-side branch.
    """

    role: Literal["user", "assistant", "tool", "system"]
    content: list[ContentBlock] = Field(default_factory=list)
    version: int = CURRENT_VERSION


# ── small helpers ────────────────────────────────────────────────────────────


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _coerce_block(raw: Any) -> ContentBlock | None:
    """Translate a single provider-native content block into a ContentBlock.

    Returns ``None`` for blocks the canonical IR does not model (e.g.
    DeepSeek's ``openai_assistant_message`` carrier or ``reasoning_content``);
    callers drop them.
    """
    block_type = _attr(raw, "type")
    if block_type == "text":
        return TextBlock(text=str(_attr(raw, "text", "") or ""))
    if block_type == "tool_use":
        return ToolUseBlock(
            id=str(_attr(raw, "id", "") or ""),
            name=str(_attr(raw, "name", "") or ""),
            input=dict(_attr(raw, "input", {}) or {}),
        )
    if block_type == "tool_result":
        content_val = _attr(raw, "content", "")
        if isinstance(content_val, list):
            # Anthropic accepts a list of text-shaped blocks for tool_result
            # content; canonicalise to a single string.
            parts: list[str] = []
            for c in content_val:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(str(c.get("text") or ""))
                else:
                    parts.append(str(c))
            content_str = "\n".join(parts)
        else:
            content_str = "" if content_val is None else str(content_val)
        return ToolResultBlock(
            tool_use_id=str(_attr(raw, "tool_use_id", "") or ""),
            content=content_str,
            is_error=bool(_attr(raw, "is_error", False) or False),
        )
    return None


# ── from-provider converters (response → IR) ────────────────────────────────


def from_anthropic_response(response: Any) -> InternalMessage:
    """Convert an Anthropic ``messages.create`` response to an InternalMessage.

    Anthropic responses already use the ``text`` / ``tool_use`` block shape
    that the canonical IR mirrors, so this is mostly a per-block coercion.
    Non-text/tool_use blocks (e.g. provider extras) are dropped.
    """
    raw_blocks = _attr(response, "content", []) or []
    content: list[ContentBlock] = []
    for raw in raw_blocks:
        block = _coerce_block(raw)
        if block is not None:
            content.append(block)
    return InternalMessage(role="assistant", content=content)


def from_deepseek_response(response: Any) -> InternalMessage:
    """Convert a DeepSeek-shaped response to an InternalMessage.

    The expected shape is what ``app.services.deepseek._to_anthropic_like_response``
    produces: a ``SimpleNamespace`` (or dict) with a ``content`` list of
    blocks including ``text``, ``tool_use``, and the DeepSeek-only
    ``openai_assistant_message`` / ``reasoning_content`` carriers.  The
    carriers are dropped on import — the canonical IR represents what was
    *said*, not the raw upstream envelope.
    """
    raw_blocks = _attr(response, "content", []) or []
    content: list[ContentBlock] = []
    for raw in raw_blocks:
        block_type = _attr(raw, "type")
        if block_type in {"openai_assistant_message", "reasoning_content"}:
            continue
        block = _coerce_block(raw)
        if block is not None:
            content.append(block)
    return InternalMessage(role="assistant", content=content)


# ── to-provider converters (IR → request payload) ───────────────────────────


def _block_to_anthropic_dict(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.input),
        }
    if isinstance(block, ToolResultBlock):
        out: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
        }
        if block.is_error:
            out["is_error"] = True
        return out
    raise TypeError(f"unsupported ContentBlock: {type(block)!r}")


def to_anthropic_request(messages: list[InternalMessage]) -> list[dict[str, Any]]:
    """Render canonical messages as an Anthropic ``messages=[...]`` payload.

    Anthropic does NOT have a 'tool' role; ToolResultBlocks must be emitted
    under a ``user`` role.  We rewrite ``role='tool'`` messages to ``user``
    at the boundary.  ``system`` messages are returned as-is so the caller
    can hoist them into the ``system=`` request kwarg.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = "user" if msg.role == "tool" else msg.role
        out.append(
            {
                "role": role,
                "content": [_block_to_anthropic_dict(b) for b in msg.content],
            }
        )
    return out


def to_deepseek_request(messages: list[InternalMessage]) -> list[dict[str, Any]]:
    """Render canonical messages as an OpenAI/DeepSeek-shaped payload.

    DeepSeek (and OpenAI) want:
      * ``role='assistant'`` with optional ``content`` text AND optional
        ``tool_calls`` array (one per ToolUseBlock).
      * ``role='tool'`` with a ``tool_call_id`` + a string ``content`` for
        each ToolResultBlock.
      * ``role='user'`` / ``'system'`` with a string ``content``.

    A canonical assistant message with mixed text + tool_use blocks emits
    one row with both ``content`` and ``tool_calls`` set; a canonical user
    message containing ToolResultBlocks emits one ``tool`` row per result
    PLUS one ``user`` row if any TextBlocks are present.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if block.text:
                        text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(block.input),
                            },
                        }
                    )
                # ToolResultBlock under assistant is a structural error; skip.
            row: dict[str, Any] = {
                "role": "assistant",
                "content": "\n\n".join(text_parts) if text_parts else None,
            }
            if tool_calls:
                row["tool_calls"] = tool_calls
            out.append(row)
            continue

        if msg.role in {"user", "tool"}:
            text_parts = []
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.tool_use_id,
                            "content": block.content,
                        }
                    )
                elif isinstance(block, TextBlock):
                    if block.text:
                        text_parts.append(block.text)
                # ToolUseBlock under user is a structural error; skip.
            if text_parts:
                out.append(
                    {"role": "user", "content": "\n\n".join(text_parts)}
                )
            continue

        if msg.role == "system":
            text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
            out.append(
                {"role": "system", "content": "\n\n".join(text_parts)}
            )
            continue

    return out


__all__ = [
    "CURRENT_VERSION",
    "ContentBlock",
    "InternalMessage",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "from_anthropic_response",
    "from_deepseek_response",
    "to_anthropic_request",
    "to_deepseek_request",
]
