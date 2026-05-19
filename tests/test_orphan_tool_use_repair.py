"""Tests for ``_pair_orphan_tool_uses_with_stubs`` — the helper that prevents
unpaired ``tool_use`` blocks from leaking past the agentic loop and 400'ing
provider calls.

Context: the agentic loop's ``read``/``consult`` iteration-cap early return
path used to leave assistant messages with ``tool_use`` blocks but no
following ``tool_result`` blocks. The next step's provider call then sent
that orphan, Anthropic returned 400 ``"tool_use ids were found without
tool_result blocks immediately after"``, the chain exhausted, and the bot
turn got mislabeled as ``"llm_timeout"``. The repair helper synthesizes
stub ``tool_result`` blocks so the conversation-history invariant always
holds before any provider call.
"""

from __future__ import annotations

import json

from app.services.agentic import _pair_orphan_tool_uses_with_stubs


def test_no_messages_returns_empty_no_repairs() -> None:
    out, count = _pair_orphan_tool_uses_with_stubs([])
    assert out == []
    assert count == 0


def test_no_tool_use_blocks_is_noop() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        {"role": "user", "content": "ok"},
    ]
    out, count = _pair_orphan_tool_uses_with_stubs(msgs)
    assert count == 0
    assert out == msgs


def test_paired_tool_use_and_tool_result_is_noop() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "id": "tu_1", "name": "lookup", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "{}"},
            ],
        },
    ]
    out, count = _pair_orphan_tool_uses_with_stubs(msgs)
    assert count == 0
    assert out == msgs


def test_orphan_tool_use_with_no_following_message_gets_stub_appended() -> None:
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_orphan", "name": "lookup", "input": {}},
            ],
        },
    ]
    out, count = _pair_orphan_tool_uses_with_stubs(msgs, reason="cap")
    assert count == 1
    assert len(out) == 2
    assert out[0] == msgs[0]
    stub_msg = out[1]
    assert stub_msg["role"] == "user"
    assert isinstance(stub_msg["content"], list)
    stub_block = stub_msg["content"][0]
    assert stub_block["type"] == "tool_result"
    assert stub_block["tool_use_id"] == "tu_orphan"
    assert stub_block["is_error"] is True
    payload = json.loads(stub_block["content"])
    assert payload["skipped"] is True
    assert payload["reason"] == "cap"


def test_orphan_when_next_message_is_user_text_splices_stub_into_content() -> None:
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_orphan", "name": "lookup", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "next step"}]},
    ]
    out, count = _pair_orphan_tool_uses_with_stubs(msgs)
    assert count == 1
    assert len(out) == 2
    next_msg = out[1]
    assert next_msg["role"] == "user"
    assert isinstance(next_msg["content"], list)
    types = [b["type"] for b in next_msg["content"]]
    assert types == ["text", "tool_result"]
    assert next_msg["content"][1]["tool_use_id"] == "tu_orphan"


def test_orphan_when_next_message_is_user_string_appends_new_message() -> None:
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_orphan", "name": "lookup", "input": {}},
            ],
        },
        {"role": "user", "content": "string-shape next message"},
    ]
    out, count = _pair_orphan_tool_uses_with_stubs(msgs)
    assert count == 1
    assert len(out) == 3
    assert out[1]["role"] == "user"
    assert isinstance(out[1]["content"], list)
    assert out[1]["content"][0]["type"] == "tool_result"
    assert out[2] == msgs[1]


def test_multiple_tool_uses_only_orphans_get_stubs() -> None:
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_paired", "name": "a", "input": {}},
                {"type": "tool_use", "id": "tu_orphan_1", "name": "b", "input": {}},
                {"type": "tool_use", "id": "tu_orphan_2", "name": "c", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_paired", "content": "{}"},
            ],
        },
    ]
    out, count = _pair_orphan_tool_uses_with_stubs(msgs)
    assert count == 2
    next_msg = out[1]
    result_ids = [b["tool_use_id"] for b in next_msg["content"]]
    assert result_ids == ["tu_paired", "tu_orphan_1", "tu_orphan_2"]


def test_repeated_repair_is_idempotent() -> None:
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_orphan", "name": "lookup", "input": {}},
            ],
        },
    ]
    once, count_1 = _pair_orphan_tool_uses_with_stubs(msgs)
    assert count_1 == 1
    twice, count_2 = _pair_orphan_tool_uses_with_stubs(once)
    assert count_2 == 0
    assert twice == once


def test_input_is_not_mutated() -> None:
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_orphan", "name": "lookup", "input": {}},
            ],
        },
    ]
    original_len = len(msgs)
    original_content_len = len(msgs[0]["content"])
    _pair_orphan_tool_uses_with_stubs(msgs)
    assert len(msgs) == original_len
    assert len(msgs[0]["content"]) == original_content_len


def test_string_content_messages_are_passed_through() -> None:
    msgs = [
        {"role": "user", "content": "plain text"},
        {"role": "assistant", "content": "another plain text"},
    ]
    out, count = _pair_orphan_tool_uses_with_stubs(msgs)
    assert count == 0
    assert out == msgs


def test_orphan_inside_multi_block_assistant_message() -> None:
    """Text + reasoning blocks alongside a tool_use don't hide the orphan."""
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "thinking..."},
                {"type": "reasoning_content", "text": "internal"},
                {"type": "tool_use", "id": "tu_orphan", "name": "lookup", "input": {}},
            ],
        },
    ]
    out, count = _pair_orphan_tool_uses_with_stubs(msgs)
    assert count == 1
    assert out[1]["content"][0]["tool_use_id"] == "tu_orphan"
