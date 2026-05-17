"""Round-trip provider-fallback sanitization tests (Project B work item 4).

The two real fallback paths in production:

1. **DeepSeek → Anthropic** (with tool-call history).
   When a DeepSeek primary run fails partway through a turn, the message
   chain accumulated so far may include DeepSeek-native / OpenAI-shaped
   content blocks (``openai_assistant_message``, ``reasoning_content``)
   that Anthropic's API will reject. The boundary helper
   :func:`app.services.agentic._anthropic_safe_messages` strips those
   blocks before re-sending into Anthropic.

2. **Anthropic → DeepSeek** (with tool-result history).
   This direction is REJECTED at the chain-resolution layer rather than
   sanitised: the deduped chain ``("anthropic", "deepseek")`` raises
   :class:`UnsupportedChainAnthropicToDeepseek` (see A2 plan v4 §105
   and commit a711d49's reject path). The test below pins that contract.

These are unit tests against the sanitization functions A2 landed on the
current branch (see commit bf5c22d). They intentionally do not exercise
the full ``_create_message_with_retry`` loop — that requires a live
Anthropic client; here we only verify the message-shape boundary.
"""

from __future__ import annotations

import copy

import pytest

from app.services.agentic import (
    UnsupportedChainAnthropicToDeepseek,
    _anthropic_safe_messages,
)


# ---------------------------------------------------------------------------
# DeepSeek → Anthropic: provider-native blocks are stripped at the boundary.
# ---------------------------------------------------------------------------


def test_strip_openai_assistant_message_block() -> None:
    """Anthropic cannot accept blocks of type ``openai_assistant_message``."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "openai_assistant_message",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "noop", "arguments": "{}"},
                        }
                    ],
                },
                {"type": "text", "text": "thinking..."},
            ],
        },
    ]
    sanitized = _anthropic_safe_messages(messages)

    # The user message is preserved as-is.
    assert sanitized[0] == {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    # The assistant message is preserved, but the offending block is gone.
    assert sanitized[1]["role"] == "assistant"
    types = [block["type"] for block in sanitized[1]["content"]]
    assert "openai_assistant_message" not in types
    assert "text" in types


def test_strip_reasoning_content_block() -> None:
    """``reasoning_content`` is the DeepSeek-native chain-of-thought block."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "reasoning_content", "text": "<thought trace>"},
                {"type": "text", "text": "final answer"},
            ],
        }
    ]
    sanitized = _anthropic_safe_messages(messages)
    types = [block["type"] for block in sanitized[0]["content"]]
    assert "reasoning_content" not in types
    assert types == ["text"]


def test_tool_call_history_round_trip_drops_provider_native_but_keeps_tool_use() -> None:
    """A realistic DeepSeek → Anthropic hand-off.

    The chain includes a normal user turn, an assistant turn that used a
    tool, the tool result, and then the assistant turn at the moment of
    the primary failure — which carries provider-native blocks because
    DeepSeek had been speaking. After sanitization, Anthropic must see a
    chain it can accept: ``tool_use``/``tool_result`` blocks survive,
    provider-native blocks do not.
    """
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "what time is it?"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "get_time",
                    "input": {},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "12:30",
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                # Primary failure occurred *inside* this assistant turn —
                # DeepSeek-native blocks are mixed in.
                {"type": "reasoning_content", "text": "i should answer"},
                {
                    "type": "openai_assistant_message",
                    "tool_calls": [],
                },
                {"type": "text", "text": "it is 12:30"},
            ],
        },
    ]
    original = copy.deepcopy(messages)
    sanitized = _anthropic_safe_messages(messages)

    # Sanitizer must not mutate its input.
    assert messages == original

    assert [m["role"] for m in sanitized] == ["user", "assistant", "user", "assistant"]
    # tool_use / tool_result survive.
    assert sanitized[1]["content"][0]["type"] == "tool_use"
    assert sanitized[2]["content"][0]["type"] == "tool_result"
    # The last assistant message has neither provider-native block.
    final_types = [b["type"] for b in sanitized[3]["content"]]
    assert "reasoning_content" not in final_types
    assert "openai_assistant_message" not in final_types
    assert final_types == ["text"]


def test_messages_with_string_content_pass_through_unchanged() -> None:
    """Non-list content (legacy plain strings) is preserved verbatim."""
    messages = [
        {"role": "user", "content": "plain string body"},
        {"role": "assistant", "content": ""},
    ]
    sanitized = _anthropic_safe_messages(messages)
    assert sanitized == messages


def test_messages_with_only_provider_native_blocks_are_dropped() -> None:
    """If filtering empties a message's content list entirely, the whole
    message is dropped — Anthropic rejects empty content arrays."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "ok"}]},
        {
            "role": "assistant",
            "content": [{"type": "reasoning_content", "text": "..."}],
        },
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
    ]
    sanitized = _anthropic_safe_messages(messages)
    assert len(sanitized) == 2
    assert [m["content"][0]["text"] for m in sanitized] == ["ok", "next"]


# ---------------------------------------------------------------------------
# Anthropic → DeepSeek: rejected at chain resolution, not sanitised.
# ---------------------------------------------------------------------------


def test_anthropic_to_deepseek_chain_raises_unsupported() -> None:
    """The reverse direction is a config error — not a sanitization path.

    The class carries a ``failure_reason`` that maps to ``infra_bug`` in
    ``FAILURE_REASON_TO_CLASS`` (per A2 plan v4 §105).  Pin the contract
    so callers don't accidentally start trying to sanitise this direction.
    """
    err = UnsupportedChainAnthropicToDeepseek()
    assert err.failure_reason == "unsupported_chain_anthropic_to_deepseek"
    # And it's-an LLMPhaseError so the outer handler classifies it.
    from app.services.agentic import LLMPhaseError

    assert isinstance(err, LLMPhaseError)
