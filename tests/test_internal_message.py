"""Round-trip tests for the canonical InternalMessage IR (Project C, C1).

We don't have ``hypothesis`` in the test deps, so these tests are
hand-crafted across the cells of the matrix:

* anthropic-shaped response  →  IR  →  anthropic request shape
* deepseek-shaped response   →  IR  →  deepseek request shape
* anthropic-shaped response  →  IR  →  deepseek request shape   (cross-provider)
* deepseek-shaped response   →  IR  →  anthropic request shape  (cross-provider)
* IR with mixed text + tool_use content
* Edge cases: empty content; only tool_use; only tool_result; version field

The contract under test is:
  1. Converters never raise on the documented input shapes.
  2. Provider-native carriers (``openai_assistant_message`` /
     ``reasoning_content``) are dropped on import — they do NOT appear in
     the IR or in any downstream conversion.
  3. The IR's ``version`` field round-trips through ``model_dump`` /
     ``model_validate``.
  4. Anthropic does not have a ``tool`` role; canonical ``tool`` messages
     emit as ``user`` in the Anthropic request shape.
  5. ToolResultBlock content survives a deepseek round-trip as a
     ``role='tool'`` row with ``tool_call_id`` preserved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.llm.internal_message import (
    CURRENT_VERSION,
    InternalMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    from_anthropic_response,
    from_deepseek_response,
    to_anthropic_request,
    to_deepseek_request,
)


# ── builders ─────────────────────────────────────────────────────────────────


def _anthropic_response(*blocks: dict | SimpleNamespace) -> SimpleNamespace:
    """Anthropic SDK response shape: an object with .content -> [blocks]."""
    return SimpleNamespace(
        content=[
            SimpleNamespace(**b) if isinstance(b, dict) else b for b in blocks
        ],
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        stop_reason="end_turn",
    )


def _deepseek_response(*blocks: dict | SimpleNamespace) -> SimpleNamespace:
    """DeepSeek-shaped response (post-_to_anthropic_like_response).

    Includes the provider-only carriers so we can assert they get dropped.
    """
    return SimpleNamespace(
        content=[
            SimpleNamespace(**b) if isinstance(b, dict) else b for b in blocks
        ],
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        stop_reason="end_turn",
    )


# ── tests ────────────────────────────────────────────────────────────────────


def test_anthropic_out_to_ir_to_anthropic_in_text_only() -> None:
    resp = _anthropic_response({"type": "text", "text": "hello world"})
    ir = from_anthropic_response(resp)
    assert ir.role == "assistant"
    assert ir.version == CURRENT_VERSION
    assert len(ir.content) == 1
    assert isinstance(ir.content[0], TextBlock)
    assert ir.content[0].text == "hello world"

    out = to_anthropic_request([ir])
    assert out == [
        {"role": "assistant", "content": [{"type": "text", "text": "hello world"}]}
    ]


def test_anthropic_out_to_ir_to_anthropic_in_with_tool_use() -> None:
    resp = _anthropic_response(
        {"type": "text", "text": "let me check"},
        {
            "type": "tool_use",
            "id": "toolu_01ABC",
            "name": "list_open_asks",
            "input": {"limit": 5},
        },
    )
    ir = from_anthropic_response(resp)
    assert len(ir.content) == 2
    assert isinstance(ir.content[1], ToolUseBlock)
    assert ir.content[1].id == "toolu_01ABC"
    assert ir.content[1].name == "list_open_asks"
    assert ir.content[1].input == {"limit": 5}

    out = to_anthropic_request([ir])
    assert out[0]["content"][1] == {
        "type": "tool_use",
        "id": "toolu_01ABC",
        "name": "list_open_asks",
        "input": {"limit": 5},
    }


def test_deepseek_out_to_ir_drops_provider_carriers() -> None:
    """openai_assistant_message + reasoning_content MUST be dropped."""
    resp = _deepseek_response(
        {
            "type": "openai_assistant_message",
            "message": {"role": "assistant", "content": "raw upstream"},
        },
        {"type": "reasoning_content", "reasoning_content": "internal scratch"},
        {"type": "text", "text": "final answer"},
        {
            "type": "tool_use",
            "id": "call_xyz",
            "name": "read_state",
            "input": {"user": "alice"},
        },
    )
    ir = from_deepseek_response(resp)
    # The carriers are gone.
    assert all(
        not isinstance(b, dict) and b.type in {"text", "tool_use", "tool_result"}
        for b in ir.content
    )
    # We kept exactly the text + tool_use.
    assert [b.type for b in ir.content] == ["text", "tool_use"]
    assert ir.content[0].text == "final answer"
    assert ir.content[1].id == "call_xyz"


def test_deepseek_out_to_ir_to_deepseek_in_round_trip() -> None:
    resp = _deepseek_response(
        {"type": "text", "text": "ok"},
        {
            "type": "tool_use",
            "id": "call_001",
            "name": "lookup",
            "input": {"q": "kale"},
        },
    )
    ir = from_deepseek_response(resp)
    out = to_deepseek_request([ir])
    # One assistant row with content + tool_calls.
    assert len(out) == 1
    row = out[0]
    assert row["role"] == "assistant"
    assert row["content"] == "ok"
    assert len(row["tool_calls"]) == 1
    tc = row["tool_calls"][0]
    assert tc["id"] == "call_001"
    assert tc["function"]["name"] == "lookup"
    assert json.loads(tc["function"]["arguments"]) == {"q": "kale"}


def test_cross_provider_anthropic_out_to_deepseek_in() -> None:
    """Anthropic-shape response -> IR -> deepseek request.

    This is the path that exists today implicitly when a fallback chain
    flips providers; the IR makes the conversion explicit and lossless for
    the modelled block types.
    """
    resp = _anthropic_response(
        {"type": "text", "text": "running tool"},
        {
            "type": "tool_use",
            "id": "toolu_A",
            "name": "search",
            "input": {"query": "x"},
        },
    )
    ir = from_anthropic_response(resp)
    out = to_deepseek_request([ir])
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "running tool"
    assert out[0]["tool_calls"][0]["id"] == "toolu_A"


def test_cross_provider_deepseek_out_to_anthropic_in() -> None:
    """DeepSeek-shape response -> IR -> anthropic request.

    Provider carriers MUST be dropped before they reach the Anthropic
    payload; otherwise Anthropic 400s on unknown block types (the bug A2
    sanitization was created to guard against).
    """
    resp = _deepseek_response(
        {
            "type": "openai_assistant_message",
            "message": {"role": "assistant", "content": "should not leak"},
        },
        {"type": "text", "text": "clean text"},
    )
    ir = from_deepseek_response(resp)
    out = to_anthropic_request([ir])
    # No openai_assistant_message block in the Anthropic payload.
    block_types = [b["type"] for b in out[0]["content"]]
    assert "openai_assistant_message" not in block_types
    assert "reasoning_content" not in block_types
    assert block_types == ["text"]


def test_ir_mixed_tool_use_and_text_to_both_providers() -> None:
    """Hand-crafted IR with mixed content survives both renderers."""
    ir = InternalMessage(
        role="assistant",
        content=[
            TextBlock(text="thinking..."),
            ToolUseBlock(id="tu_1", name="a", input={"k": 1}),
            TextBlock(text="...done"),
            ToolUseBlock(id="tu_2", name="b", input={}),
        ],
    )
    anthropic_out = to_anthropic_request([ir])
    assert len(anthropic_out[0]["content"]) == 4
    assert [b["type"] for b in anthropic_out[0]["content"]] == [
        "text",
        "tool_use",
        "text",
        "tool_use",
    ]

    deepseek_out = to_deepseek_request([ir])
    # DeepSeek collapses text-parts and emits all tool_calls in one row.
    assert deepseek_out[0]["role"] == "assistant"
    assert "thinking..." in (deepseek_out[0]["content"] or "")
    assert "...done" in (deepseek_out[0]["content"] or "")
    assert [tc["id"] for tc in deepseek_out[0]["tool_calls"]] == ["tu_1", "tu_2"]


def test_ir_empty_content() -> None:
    ir = InternalMessage(role="assistant", content=[])
    assert to_anthropic_request([ir]) == [{"role": "assistant", "content": []}]
    # DeepSeek: no text and no tool_calls — still emits an assistant row with
    # ``content=None`` (OpenAI accepts this when ``tool_calls`` is set, but
    # also tolerates it for a completion-less message).
    out = to_deepseek_request([ir])
    assert out == [{"role": "assistant", "content": None}]


def test_ir_only_tool_use() -> None:
    ir = InternalMessage(
        role="assistant",
        content=[ToolUseBlock(id="x", name="t", input={"a": "b"})],
    )
    anthropic_out = to_anthropic_request([ir])
    assert anthropic_out[0]["content"] == [
        {"type": "tool_use", "id": "x", "name": "t", "input": {"a": "b"}}
    ]
    deepseek_out = to_deepseek_request([ir])
    assert deepseek_out[0]["content"] is None
    assert deepseek_out[0]["tool_calls"][0]["function"]["name"] == "t"


def test_ir_only_tool_result_user_role() -> None:
    """ToolResultBlocks live under role='user' for Anthropic and emit
    one ``role='tool'`` row each for DeepSeek."""
    ir = InternalMessage(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="tu_1", content="ok"),
            ToolResultBlock(tool_use_id="tu_2", content="boom", is_error=True),
        ],
    )
    anthropic_out = to_anthropic_request([ir])
    assert anthropic_out[0]["role"] == "user"
    assert anthropic_out[0]["content"] == [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
        {
            "type": "tool_result",
            "tool_use_id": "tu_2",
            "content": "boom",
            "is_error": True,
        },
    ]

    deepseek_out = to_deepseek_request([ir])
    assert [r["role"] for r in deepseek_out] == ["tool", "tool"]
    assert deepseek_out[0] == {"role": "tool", "tool_call_id": "tu_1", "content": "ok"}
    assert deepseek_out[1]["tool_call_id"] == "tu_2"


def test_tool_role_renders_as_user_for_anthropic() -> None:
    """Canonical role='tool' rewrites to 'user' for Anthropic."""
    ir = InternalMessage(
        role="tool",
        content=[ToolResultBlock(tool_use_id="t", content="result")],
    )
    out = to_anthropic_request([ir])
    assert out[0]["role"] == "user"


def test_version_field_round_trips() -> None:
    """version is preserved across model_dump / model_validate."""
    ir = InternalMessage(
        role="assistant",
        content=[TextBlock(text="hi")],
    )
    dumped = ir.model_dump()
    assert dumped["version"] == CURRENT_VERSION
    rebuilt = InternalMessage.model_validate(dumped)
    assert rebuilt.version == CURRENT_VERSION
    assert rebuilt.role == "assistant"
    assert rebuilt.content[0].text == "hi"
    # Explicit non-default also survives.
    ir2 = InternalMessage(role="assistant", content=[], version=2)
    assert InternalMessage.model_validate(ir2.model_dump()).version == 2


def test_anthropic_tool_result_with_list_content_canonicalises_to_string() -> None:
    """Anthropic permits tool_result.content to be a list of text-shaped
    blocks; the IR coerces this to a single newline-joined string."""
    resp = _anthropic_response(
        # tool_result blocks normally appear in user messages, but the
        # converter handles them uniformly via _coerce_block.
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}],
        },
    )
    ir = from_anthropic_response(resp)
    assert isinstance(ir.content[0], ToolResultBlock)
    assert ir.content[0].content == "line1\nline2"


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
