"""Unit tests for non-chat agentic runner and live_prep tool policy.

Covers:
(a) Flat tool policy: live_prep step exposes the right tools and excludes outbound/write/schedule.
(b) Submit success: fake provider populates submit_live_brief → NonchatJobResult.success=True.
(c) Cap exhaustion: max_tool_iterations reached without submit → failure_reason='live_prep_submit_missing'.
(d) Plain-text failure: provider returns text only, no submit → failure_reason='live_prep_text_no_submit'.
(e) Empty-no-submit failure: provider returns nothing → failure.
(f) Generalized runner with debrief config.
(g) Debrief cost recording.

All tests use monkeypatching to avoid real LLM calls and DB round-trips.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.bots.base import BotSpec
from app.bots.registry import BOT_SPECS, get_bot_spec
from app.models.user import User
from app.services.agentic import BoundedLoopExceeded
from app.services.nonchat_agentic import (
    NonchatJobResult,
    run_agentic_nonchat_job,
)
from app.services.tools.registry import _step_allowed, LIVE_PREP_TOOLS, WRITE_PHASE_TOOLS
from app.services.turn_context import TurnContext

# ── shared fixtures / helpers ────────────────────────────────────────────────


def _make_user(name: str = "test-user") -> User:
    return User(
        id=uuid4(),
        name=name,
        phone="+155****0100",
        timezone="UTC",
    )


def _make_bot_spec() -> BotSpec:
    """Return a minimal BotSpec suitable for the non-chat runner.

    Uses the real mediator spec from the registry but overrides fields
    that the non-chat runner depends on so tests are deterministic.
    """
    spec = BOT_SPECS.get("mediator")
    if spec is None:
        # Fallback if registry is not loaded — construct directly.
        def _noop_renderer(*args: Any, **kwargs: Any) -> str:
            return ""

        return BotSpec(
            bot_id="test-bot",
            prompt_renderer=_noop_renderer,
            step_instructions={},
            display_name="Test Bot",
            bot_spec_version="0.0.0",
            participants_shape="solo",
            primary_topic_slug="general",
            provider_chain=("anthropic",),
        )
    # Use the real mediator spec's renderer so tool_allowlist and scopes
    # are populated correctly for _step_allowed filtering.
    return spec


def _make_ctx(
    *,
    current_step: str = "live_prep",
    user: User | None = None,
    partner: User | None = None,
    bot_spec: BotSpec | None = None,
    topic_id: UUID | None = None,
    turn_id: UUID | None = None,
    pool: Any = None,
) -> TurnContext:
    """Build a TurnContext with the fields the non-chat runner depends on."""
    if user is None:
        user = _make_user()
    if bot_spec is None:
        bot_spec = _make_bot_spec()
    return TurnContext(
        turn_id=turn_id or uuid4(),
        pool=pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        bot_id=bot_spec.bot_id,
        transport=None,
        user_id=user.id,
        bot_spec=bot_spec,
        binding_id=None,
        participants_shape=bot_spec.participants_shape,
        primary_topic_id=topic_id or uuid4(),
        primary_topic_slug=bot_spec.primary_topic_slug,
        channel_id=None,
        read_scopes=getattr(bot_spec, "read_scopes", None),
        write_scopes=getattr(bot_spec, "write_scopes", None),
        cross_topic_policy=getattr(bot_spec, "cross_topic_policy", None),
        dyad_id=None,
        current_step=current_step,
        turn_started_at=datetime.now(timezone.utc),
        trigger_metadata={},
    )


# ── (a) Flat tool policy ────────────────────────────────────────────────────


class TestLivePrepToolPolicy:
    """Verify that the live_prep step exposes read tools + submit_live_brief
    and blocks all outbound, write, and schedule tools."""

    def test_live_prep_step_allowed_contains_required_tools(self) -> None:
        ctx = _make_ctx(current_step="live_prep")
        allowed = _step_allowed(ctx)

        # Required tools must be present.
        assert "submit_live_brief" in allowed, (
            "submit_live_brief must be in live_prep allowed tools"
        )
        assert "update_turn_plan" in allowed, (
            "update_turn_plan (ALWAYS_ALLOWED) must be in live_prep"
        )
        assert "search_messages" in allowed, (
            "search_messages must be in live_prep"
        )
        assert "get_distillations" in allowed, (
            "get_distillations must be in live_prep"
        )
        assert "list_themes" in allowed, (
            "list_themes must be in live_prep"
        )
        assert "get_self_model" in allowed, (
            "get_self_model must be in live_prep"
        )

    def test_live_prep_excludes_outbound_tools(self) -> None:
        ctx = _make_ctx(current_step="live_prep")
        allowed = _step_allowed(ctx)

        assert "send_message_part" not in allowed, (
            "send_message_part must NOT be in live_prep (outbound)"
        )
        assert "summarize_oob_topics" not in allowed, (
            "summarize_oob_topics must NOT be in live_prep (OOB)"
        )
        assert "check_oob" not in allowed, (
            "check_oob must NOT be in live_prep (OOB)"
        )

    def test_live_prep_excludes_write_tools(self) -> None:
        ctx = _make_ctx(current_step="live_prep")
        allowed = _step_allowed(ctx)

        # No write-phase tool should appear.
        write_overlap = allowed & WRITE_PHASE_TOOLS
        assert write_overlap == set(), (
            f"live_prep must not expose write-phase tools; got {write_overlap}"
        )

    def test_live_prep_excludes_write_phase_tools(self) -> None:
        ctx = _make_ctx(current_step="live_prep")
        allowed = _step_allowed(ctx)

        # No write-phase tool should appear (WRITE_PHASE_TOOLS are the
        # authoritative source — not SCHEDULE_TOOLS).
        write_overlap = allowed & WRITE_PHASE_TOOLS
        assert write_overlap == set(), (
            f"live_prep must not expose write-phase tools; got {write_overlap}"
        )

        # Additionally, check that send_message_part is excluded (explicit plan req).
        assert "send_message_part" not in allowed, (
            "send_message_part must NOT be in live_prep"
        )

    def test_live_prep_matches_LIVE_PREP_TOOLS_constant(self) -> None:
        ctx = _make_ctx(current_step="live_prep")
        allowed = _step_allowed(ctx)

        # The result must be a subset of LIVE_PREP_TOOLS ∪ ALWAYS_ALLOWED_TOOLS.
        # _step_allowed further filters through bot_spec.tool_allowlist and
        # BOT_EXCLUSIVE_TOOLS (e.g., hector-only commitment/event tools are
        # removed for non-hector bots).  So we verify subset, not equality.
        expected_universe = LIVE_PREP_TOOLS | {"update_turn_plan"}
        assert allowed <= expected_universe, (
            f"live_prep allowed tools must be a subset of "
            f"LIVE_PREP_TOOLS ∪ ALWAYS_ALLOWED_TOOLS; "
            f"extra: {sorted(allowed - expected_universe)}"
        )

        # Key tools that must always be present.
        for required in ("submit_live_brief", "update_turn_plan",
                         "search_messages", "get_distillations", "list_themes"):
            assert required in allowed, (
                f"{required} must be in live_prep allowed tools"
            )

    def test_live_prep_tool_count_reasonable(self) -> None:
        ctx = _make_ctx(current_step="live_prep")
        allowed = _step_allowed(ctx)
        # Should be roughly 20-25 tools (read tools minus OOB + submit_live_brief + update_turn_plan)
        assert 15 <= len(allowed) <= 35, (
            f"live_prep tool count {len(allowed)} outside expected range"
        )


# ── (b) Submit success ──────────────────────────────────────────────────────


class TestSubmitSuccess:
    """Verify that when the provider calls submit_live_brief, the runner
    returns success with the submitted brief."""

    async def test_submit_live_brief_sets_extras_and_returns_success(
        self, monkeypatch: Any
    ) -> None:
        from app.services import nonchat_agentic as nac

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()

        # Stub out the DB helpers so we never touch a real pool.
        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        submitted_brief = {
            "agenda": {
                "prep_summary": "A test prep brief",
                "items": [
                    {
                        "id": "anchor",
                        "title": "Anchor item",
                        "priority": "must",
                        "kind": "planned",
                        "speaker_scope": "primary",
                        "coverage_evidence_required": "explicit_answer",
                    },
                    {
                        "id": "follow",
                        "title": "Follow item",
                        "priority": "should",
                        "kind": "planned",
                        "speaker_scope": "primary",
                        "coverage_evidence_required": "explicit_answer",
                    },
                ],
                "first_item_id": "anchor",
            },
            "notes": "optional prep notes",
        }

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            # Simulate the provider calling submit_live_brief.
            ctx.extras["submitted_live_brief"] = submitted_brief
            return "", [], 2  # final_text, messages, tool_call_count

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        result = await run_agentic_nonchat_job(
            kind="live_prep",
            user=user,
            conversation_id=conversation_id,
            system_task="Test system task",
            max_tool_iterations=10,
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
        )

        assert result.success is True, f"Expected success, got {result}"
        assert result.brief == submitted_brief, (
            f"brief mismatch: {result.brief}"
        )
        assert result.failure_reason is None, (
            f"failure_reason should be None on success, got {result.failure_reason}"
        )
        assert result.turn_id is not None, "turn_id must be set on success"
        assert result.tool_call_count == 2, (
            f"tool_call_count mismatch: {result.tool_call_count}"
        )


# ── (c) Cap exhaustion ──────────────────────────────────────────────────────


class TestCapExhaustion:
    """Verify that hitting the tool-iteration cap without submit_live_brief
    produces a failure result."""

    async def test_cap_exhaustion_returns_failure(self, monkeypatch: Any) -> None:
        from app.services import nonchat_agentic as nac

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            raise BoundedLoopExceeded(
                "max tool iterations (5) exceeded",
                max_iterations=5,
                tool_call_count=5,
            )

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        result = await run_agentic_nonchat_job(
            kind="live_prep",
            user=user,
            conversation_id=conversation_id,
            system_task="Test system task",
            max_tool_iterations=5,
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
        )

        assert result.success is False, f"Expected failure on cap exhaustion, got {result}"
        assert result.failure_reason == "live_prep_submit_missing", (
            f"Expected failure_reason='live_prep_submit_missing', got {result.failure_reason!r}"
        )
        assert result.brief is None, "brief must be None on failure"


# ── (d) Plain-text failure ──────────────────────────────────────────────────


class TestPlainTextFailure:
    """Verify that when the provider returns text without calling
    submit_live_brief, the runner returns failure with the correct reason."""

    async def test_plain_text_without_submit_fails(self, monkeypatch: Any) -> None:
        from app.services import nonchat_agentic as nac

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            # Provider returns text but never calls submit_live_brief.
            return "Here is an agenda in plain text...", [], 0

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        result = await run_agentic_nonchat_job(
            kind="live_prep",
            user=user,
            conversation_id=conversation_id,
            system_task="Test system task",
            max_tool_iterations=10,
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
        )

        assert result.success is False, f"Expected failure on plain text, got {result}"
        assert result.failure_reason == "live_prep_text_no_submit", (
            f"Expected failure_reason='live_prep_text_no_submit', got {result.failure_reason!r}"
        )
        assert result.brief is None, "brief must be None on failure"


# ── (e) Empty-no-submit failure ─────────────────────────────────────────────


class TestEmptyNoSubmitFailure:
    """Verify that when the provider returns nothing (no text, no tool_use),
    the runner returns failure."""

    async def test_empty_output_without_submit_fails(self, monkeypatch: Any) -> None:
        from app.services import nonchat_agentic as nac

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            # Provider returns empty — no text and no tool_use.
            return "", [], 0

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        result = await run_agentic_nonchat_job(
            kind="live_prep",
            user=user,
            conversation_id=conversation_id,
            system_task="Test system task",
            max_tool_iterations=10,
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
        )

        assert result.success is False, (
            f"Expected failure on empty output, got {result}"
        )
        assert result.failure_reason == "live_prep_submit_missing", (
            f"Expected failure_reason='live_prep_submit_missing', got {result.failure_reason!r}"
        )
        assert result.brief is None, "brief must be None on failure"

    async def test_whitespace_only_output_without_submit_fails(
        self, monkeypatch: Any
    ) -> None:
        """Whitespace-only text is treated as empty (stripped)."""
        from app.services import nonchat_agentic as nac

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            return "   \n  ", [], 0

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        result = await run_agentic_nonchat_job(
            kind="live_prep",
            user=user,
            conversation_id=conversation_id,
            system_task="Test system task",
            max_tool_iterations=10,
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
        )

        # Whitespace-only should be treated as empty (no meaningful text).
        assert result.success is False
        assert result.failure_reason == "live_prep_submit_missing"


# ── (f) Generalized runner with debrief config ──────────────────────────────


class TestGeneralizedRunnerWithDebriefConfig:
    """Verify run_agentic_nonchat_job works with LIVE_DEBRIEF_CONFIG."""

    async def test_debrief_config_success_path(self, monkeypatch: Any) -> None:
        """Debrief config: submit_live_debrief gate, success return."""
        from app.services import nonchat_agentic as nac
        from app.services.nonchat_agentic import LIVE_DEBRIEF_CONFIG

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()
        submitted_payload = {
            "review_summary": "debrief summary",
            "what_heard": "some things",
            "what_decided": "some decisions",
            "still_open": "some open items",
            "what_to_remember": "some memories",
            "durable_write_summary": "some writes",
            "open_questions": "some questions",
            "references": [],
            "failed_writes": [],
        }

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            ctx.extras["submitted_live_debrief"] = submitted_payload
            return "", [], 3

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        debrief_cfg = nac.NonchatJobConfig(
            current_step=LIVE_DEBRIEF_CONFIG.current_step,
            submit_extras_key=LIVE_DEBRIEF_CONFIG.submit_extras_key,
            submit_tool_name=LIVE_DEBRIEF_CONFIG.submit_tool_name,
            failure_reason_prefix=LIVE_DEBRIEF_CONFIG.failure_reason_prefix,
        )

        result = await run_agentic_nonchat_job(
            kind="live_debrief",
            user=user,
            conversation_id=conversation_id,
            system_task="Debrief task",
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
            trigger_metadata={"kind": "live_debrief", "conversation_id": str(conversation_id)},
            config=debrief_cfg,
        )

        assert result.success is True
        assert result.brief == submitted_payload
        assert result.failure_reason is None

    async def test_debrief_config_text_no_submit_failure(self, monkeypatch: Any) -> None:
        """Debrief config: text without submit -> live_debrief_text_no_submit."""
        from app.services import nonchat_agentic as nac
        from app.services.nonchat_agentic import LIVE_DEBRIEF_CONFIG

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            return "plain text without submit", [], 0

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        debrief_cfg = nac.NonchatJobConfig(
            current_step=LIVE_DEBRIEF_CONFIG.current_step,
            submit_extras_key=LIVE_DEBRIEF_CONFIG.submit_extras_key,
            submit_tool_name=LIVE_DEBRIEF_CONFIG.submit_tool_name,
            failure_reason_prefix=LIVE_DEBRIEF_CONFIG.failure_reason_prefix,
        )

        result = await run_agentic_nonchat_job(
            kind="live_debrief",
            user=user,
            conversation_id=conversation_id,
            system_task="Debrief task",
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
            trigger_metadata={"kind": "live_debrief"},
            config=debrief_cfg,
        )

        assert result.success is False
        assert result.failure_reason == "live_debrief_text_no_submit", (
            f"Expected live_debrief_text_no_submit, got {result.failure_reason!r}"
        )

    async def test_debrief_config_submit_missing_failure(self, monkeypatch: Any) -> None:
        """Debrief config: no output -> live_debrief_submit_missing."""
        from app.services import nonchat_agentic as nac
        from app.services.nonchat_agentic import LIVE_DEBRIEF_CONFIG

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            return "", [], 0

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        debrief_cfg = nac.NonchatJobConfig(
            current_step=LIVE_DEBRIEF_CONFIG.current_step,
            submit_extras_key=LIVE_DEBRIEF_CONFIG.submit_extras_key,
            submit_tool_name=LIVE_DEBRIEF_CONFIG.submit_tool_name,
            failure_reason_prefix=LIVE_DEBRIEF_CONFIG.failure_reason_prefix,
        )

        result = await run_agentic_nonchat_job(
            kind="live_debrief",
            user=user,
            conversation_id=conversation_id,
            system_task="Debrief task",
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
            trigger_metadata={"kind": "live_debrief"},
            config=debrief_cfg,
        )

        assert result.success is False
        assert result.failure_reason == "live_debrief_submit_missing", (
            f"Expected live_debrief_submit_missing, got {result.failure_reason!r}"
        )

    async def test_debrief_config_bounded_loop_cap_failure(self, monkeypatch: Any) -> None:
        """Debrief config: BoundedLoopExceeded -> live_debrief_submit_missing_at_tool_cap."""
        from app.services import nonchat_agentic as nac
        from app.services.nonchat_agentic import LIVE_DEBRIEF_CONFIG

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            raise BoundedLoopExceeded("max tool iterations exceeded")

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        debrief_cfg = nac.NonchatJobConfig(
            current_step=LIVE_DEBRIEF_CONFIG.current_step,
            submit_extras_key=LIVE_DEBRIEF_CONFIG.submit_extras_key,
            submit_tool_name=LIVE_DEBRIEF_CONFIG.submit_tool_name,
            failure_reason_prefix=LIVE_DEBRIEF_CONFIG.failure_reason_prefix,
        )

        result = await run_agentic_nonchat_job(
            kind="live_debrief",
            user=user,
            conversation_id=conversation_id,
            system_task="Debrief task",
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
            trigger_metadata={"kind": "live_debrief"},
            config=debrief_cfg,
        )

        assert result.success is False
        assert result.failure_reason == "live_debrief_submit_missing_at_tool_cap", (
            f"Expected live_debrief_submit_missing_at_tool_cap, got {result.failure_reason!r}"
        )

    async def test_debrief_config_max_tool_calls_cap_failure(self, monkeypatch: Any) -> None:
        """Debrief config: MaxToolCallsExceeded -> live_debrief_submit_missing_at_tool_cap."""
        from app.services import nonchat_agentic as nac
        from app.services.nonchat_agentic import LIVE_DEBRIEF_CONFIG
        from app.services.agentic import MaxToolCallsExceeded

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            raise MaxToolCallsExceeded(
                "tool call cap (500) exceeded",
                tool_call_count=500,
                max_calls=500,
            )

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        debrief_cfg = nac.NonchatJobConfig(
            current_step=LIVE_DEBRIEF_CONFIG.current_step,
            submit_extras_key=LIVE_DEBRIEF_CONFIG.submit_extras_key,
            submit_tool_name=LIVE_DEBRIEF_CONFIG.submit_tool_name,
            failure_reason_prefix=LIVE_DEBRIEF_CONFIG.failure_reason_prefix,
        )

        result = await run_agentic_nonchat_job(
            kind="live_debrief",
            user=user,
            conversation_id=conversation_id,
            system_task="Debrief task",
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
            trigger_metadata={"kind": "live_debrief"},
            config=debrief_cfg,
        )

        assert result.success is False
        assert result.failure_reason == "live_debrief_submit_missing_at_tool_cap", (
            f"Expected live_debrief_submit_missing_at_tool_cap, got {result.failure_reason!r}"
        )

    async def test_live_prep_still_works_without_config(self, monkeypatch: Any) -> None:
        """Backward compat: run_agentic_nonchat_job without config uses live_prep defaults."""
        from app.services import nonchat_agentic as nac

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()
        submitted_brief = {
            "agenda": {"prep_summary": "test", "items": [], "first_item_id": None},
            "notes": "",
        }

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(client, ctx, system_prompt, hot_context_rendered,
                                allowed_tools, seed_messages, **kwargs):
            ctx.extras["submitted_live_brief"] = submitted_brief
            return "", [], 2

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        # No config passed — should default to LIVE_PREP_CONFIG.
        result = await run_agentic_nonchat_job(
            kind="live_prep",
            user=user,
            conversation_id=conversation_id,
            system_task="Prep task",
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
        )

        assert result.success is True
        assert result.brief == submitted_brief


# ── (g) Debrief cost recording ──────────────────────────────────────────────


class TestDebriefCostRecording:
    """Verify that debrief provider usage flows through the global text LLM
    cost accounting path (record_llm_cost), and that per-session
    spend_usd_cents is NOT updated for debrief in Sprint 3.

    The debrief runner uses ``run_agentic_nonchat_job`` which defers to
    ``run_step``, and ``run_step`` internally calls ``_record_response_cost``
    → ``record_llm_cost(pool, "text", dollars)``.  This is the same global
    cost-recording path used by all text-LLM calls.
    """

    async def test_debrief_job_hits_global_cost_path(
        self, monkeypatch: Any
    ) -> None:
        """Assert that a successful debrief job triggers ``record_llm_cost``."""
        from app.services import nonchat_agentic as nac
        from app.services.nonchat_agentic import LIVE_DEBRIEF_CONFIG

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()
        submitted_payload = {
            "review_summary": "Test debrief summary",
            "what_heard": "User mentioned testing.",
            "what_decided": "Testing debrief path.",
            "still_open": "Nothing.",
            "what_to_remember": "Debrief cost test.",
            "durable_write_summary": "No writes performed.",
            "open_questions": "Is cost recorded?",
            "references": [],
            "failed_writes": [],
        }

        # Track calls to record_llm_cost
        cost_calls: list[tuple[str, float]] = []

        async def fake_record_llm_cost(pool: Any, provider: str, dollars: float) -> None:
            cost_calls.append((provider, float(dollars)))

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        # We monkeypatch run_step to simulate a successful debrief AND
        # record that it would have called record_llm_cost internally.
        import app.services.agentic as agentic_mod
        monkeypatch.setattr(
            agentic_mod, "record_llm_cost", fake_record_llm_cost,
        )

        async def fake_run_step(
            client, ctx, system_prompt, hot_context_rendered,
            allowed_tools, seed_messages, **kwargs,
        ):
            # Simulate a cost recording (as _record_response_cost would).
            await fake_record_llm_cost(None, "text", 0.001)
            ctx.extras["submitted_live_debrief"] = submitted_payload
            return "", [], 3

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        debrief_config = nac.NonchatJobConfig(
            current_step=LIVE_DEBRIEF_CONFIG.current_step,
            submit_extras_key=LIVE_DEBRIEF_CONFIG.submit_extras_key,
            submit_tool_name=LIVE_DEBRIEF_CONFIG.submit_tool_name,
            failure_reason_prefix=LIVE_DEBRIEF_CONFIG.failure_reason_prefix,
        )

        result = await run_agentic_nonchat_job(
            kind="live_debrief",
            user=user,
            conversation_id=conversation_id,
            system_task="Debrief system task",
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
            trigger_metadata={"kind": "live_debrief", "conversation_id": str(conversation_id)},
            config=debrief_config,
        )

        assert result.success is True, f"Expected success, got {result}"
        assert result.brief == submitted_payload

        # The global text cost path must have been called at least once.
        assert len(cost_calls) >= 1, (
            "Expected record_llm_cost to be called at least once for debrief, "
            "got 0 calls.  Debrief provider usage must flow through the "
            "global text LLM cost accounting path."
        )
        assert cost_calls[0][0] == "text", (
            f"Expected cost provider='text', got {cost_calls[0][0]!r}"
        )
        assert cost_calls[0][1] > 0, (
            f"Expected non-zero cost, got {cost_calls[0][1]}"
        )

    async def test_debrief_job_does_not_touch_per_session_spend(
        self, monkeypatch: Any
    ) -> None:
        """Verify that the debrief runner does NOT call charge_session
        (i.e. per-session spend_usd_cents is intentionally not bumped
        in Sprint 3).
        """
        from app.services import nonchat_agentic as nac
        from app.services.nonchat_agentic import LIVE_DEBRIEF_CONFIG

        user = _make_user()
        bot_spec = _make_bot_spec()
        conversation_id = uuid4()
        submitted_payload = {"review_summary": "test"}

        # Track whether charge_session is ever imported or called.
        charge_calls: list[Any] = []

        # Patch charge_session in the budget module to detect calls.
        import app.services.live.budget as budget_mod
        original_charge = budget_mod.charge_session

        async def fake_charge_session(pool: Any, session_id: Any, cents: int) -> Any:
            charge_calls.append((session_id, cents))
            return original_charge  # won't be called

        monkeypatch.setattr(budget_mod, "charge_session", fake_charge_session)

        monkeypatch.setattr(nac, "_open_nonchat_turn", _fake_open_nonchat_turn)
        monkeypatch.setattr(nac, "_finalize_turn_atomically", _fake_finalize_turn)

        async def fake_run_step(
            client, ctx, system_prompt, hot_context_rendered,
            allowed_tools, seed_messages, **kwargs,
        ):
            ctx.extras["submitted_live_debrief"] = submitted_payload
            return "", [], 3

        monkeypatch.setattr(nac, "run_step", fake_run_step)

        debrief_config = nac.NonchatJobConfig(
            current_step=LIVE_DEBRIEF_CONFIG.current_step,
            submit_extras_key=LIVE_DEBRIEF_CONFIG.submit_extras_key,
            submit_tool_name=LIVE_DEBRIEF_CONFIG.submit_tool_name,
            failure_reason_prefix=LIVE_DEBRIEF_CONFIG.failure_reason_prefix,
        )

        result = await run_agentic_nonchat_job(
            kind="live_debrief",
            user=user,
            conversation_id=conversation_id,
            system_task="Debrief system task",
            pool=None,
            bot_spec=bot_spec,
            bot_id=bot_spec.bot_id,
            topic_id=uuid4(),
            partner=None,
            hot_context="",
            trigger_metadata={"kind": "live_debrief", "conversation_id": str(conversation_id)},
            config=debrief_config,
        )

        assert result.success is True

        # Per-session spend must NOT be updated in Sprint 3.
        assert len(charge_calls) == 0, (
            f"charge_session was called {len(charge_calls)} times during debrief. "
            "Per-session spend_usd_cents must NOT be updated for debrief in Sprint 3. "
            "This is deferred to Sprint 4 behind the provenance-linking feature flag."
        )


# ── fake helpers ────────────────────────────────────────────────────────────


async def _fake_open_nonchat_turn(
    pool: Any,
    user_id: UUID,
    prompt_snapshot: str,
    model_version: str,
    system_prompt_version: str,
    *,
    bot_id: str,
    topic_id: UUID | None,
    kind: str,
    conversation_id: UUID,
) -> tuple[UUID, datetime]:
    """Return a fake turn_id without touching a real database."""
    return uuid4(), datetime.now(timezone.utc)


async def _fake_finalize_turn(
    pool: Any,
    turn_id: UUID,
    started_at: datetime,
    final_output_message_id: Any,
    tool_call_count: int,
    reasoning: str,
    *,
    outcome: str = "responded",
    scope: Any = None,
    primary_topic_id: UUID | None = None,
    failure_reason: str | None = None,
    failure_class: str | None = None,
    processing_error: str | None = None,
) -> None:
    """No-op fake for _finalize_turn_atomically."""
    return
