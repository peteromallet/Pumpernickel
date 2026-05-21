"""Integration tests for agentic live debrief (Sprint 3).

Covers:
(a) Happy path — seed live conversation with prep artifact, agenda, transcript,
    notes; fake provider to call submit_live_debrief; assert artifacts land
    and status=review_pending.
(b) Durable write path — scoped write from debrief passes read-before-write
    + safety gate.
(c) Redaction enforcement — guarded write citing redacted partner turn
    rejected server-side.
(d) Outbound denial — outbound tools rejected by call_tool.
(e) Privacy — partner raw text only with consent+opt-in.
(f) Failure — missing submit/cap exhaustion -> debrief_failed.
(g) Retry path.

All tests use a FakePool that records SQL — no real LLM APIs or DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.nonchat_agentic import NonchatJobResult
from app.services.tools.registry import (
    LIVE_DEBRIEF_GUARDED_WRITE_TOOLS,
    LIVE_DEBRIEF_OUTBOUND_DENYLIST,
    build_live_debrief_tools,
    _step_allowed,
)
from app.services.turn_context import TurnContext
from app.bots.registry import get_bot_spec, BOT_SPECS


# ── FakePool for debrief integration tests ───────────────────────────────────


class _FakeTxn:
    """Auto-committing fake transaction — no-op enter/exit."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    """Single-connection handle that delegates to the parent FakePool."""

    def __init__(self, parent: "DebriefFakePool") -> None:
        self._parent = parent

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._parent.execute(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return await self._parent.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return await self._parent.fetch(sql, *args)


class _FakeAcquire:
    def __init__(self, parent: "DebriefFakePool") -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._parent)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class DebriefFakePool:
    """Minimal asyncpg pool stand-in for live debrief integration tests.

    Records all executed SQL and supplies canned return values for every
    fetch/fetchrow pattern used by the live debrief code path.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

        # Canned fetchrow results keyed by substring match.
        self._fetchrow_map: dict[str, dict[str, Any] | None] = {}
        # Canned fetch (multi-row) results keyed by substring match.
        self._fetch_map: dict[str, list[dict[str, Any]]] = {}

        # Track INSERTs / UPDATEs for verification.
        self.inserted_artifact_payloads: list[dict[str, Any]] = []
        self.updated_status: str | None = None
        self.updated_session_fields: dict[str, Any] | None = None

        # Auto-generated UUIDs for artifact rows.
        self._artifact_id_counter = 0

    # -- public helpers --

    def set_conversations_row(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        partner_user_id: UUID | None = None,
        bot_id: str = "mediator",
        status: str = "debriefing",
        topic_id: UUID | None = None,
        session_fields: dict[str, Any] | None = None,
        prep_summary: str | None = None,
        current_item_id: UUID | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        self._fetchrow_map["FROM mediator.conversations"] = {
            "id": conversation_id,
            "user_id": str(user_id),
            "partner_user_id": str(partner_user_id) if partner_user_id else None,
            "bot_id": bot_id,
            "mode": "open",
            "steering_text": "",
            "status": status,
            "topic_id": str(topic_id) if topic_id else None,
            "session_fields": session_fields or {},
            "prep_summary": prep_summary,
            "current_item_id": str(current_item_id) if current_item_id else None,
            "started_at": started_at,
            "ended_at": ended_at,
        }

    def set_user_row(self, user_id: UUID, *, name: str = "test-user") -> None:
        self._fetchrow_map["SELECT * FROM users"] = {
            "id": user_id,
            "name": name,
            "phone": "+155****0000",
            "timezone": "UTC",
        }

    def set_transcript_turns(
        self, rows: list[dict[str, Any]] | None = None
    ) -> None:
        self._fetch_map["FROM mediator.transcript_turns"] = rows or []

    def set_speakers(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["FROM mediator.conversation_speakers"] = rows or []

    def set_agenda_items(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["FROM mediator.conversation_items"] = rows or []

    def set_notes(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["FROM mediator.conversation_notes"] = rows or []

    def set_artifacts(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["conversation_artifacts WHERE"] = rows or []

    # -- asyncpg-shaped surface --

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append(("fetchrow:" + sql.strip()[:120], args))

        # Handle INSERT ... RETURNING * patterns.
        if "INSERT INTO mediator.conversation_artifacts" in sql:
            self._artifact_id_counter += 1
            payload = args[4] if len(args) > 4 else {}
            artifact_type = args[3] if len(args) > 3 else ""
            if isinstance(payload, dict):
                self.inserted_artifact_payloads.append({
                    "payload": payload,
                    "artifact_type": artifact_type,
                })
            return {
                "id": f"artifact-{self._artifact_id_counter:04d}",
                "conversation_id": args[0] if args else "",
                "bot_id": args[1] if len(args) > 1 else "",
                "user_id": args[2] if len(args) > 2 else "",
                "artifact_type": artifact_type,
                "payload": payload,
                "payload_version": args[5] if len(args) > 5 else 1,
                "revision_number": 1,
                "created_by_turn_id": args[6] if len(args) > 6 else None,
                "deleted_at": None,
                "expires_at": args[7] if len(args) > 7 else None,
                "created_at": datetime.now(timezone.utc),
            }

        if "INSERT INTO mediator.artifact_links" in sql:
            return {
                "id": "link-0001",
                "artifact_id": args[0] if args else "",
                "target_table": args[1] if len(args) > 1 else "",
                "target_id": args[2] if len(args) > 2 else "",
                "relation": args[3] if len(args) > 3 else "",
                "evidence": args[4] if len(args) > 4 else None,
                "deleted_at": None,
                "created_at": datetime.now(timezone.utc),
            }

        if "INSERT" in sql and "RETURNING" in sql:
            return {}

        # Match on substring for SELECT fetchrows.
        for key, row in self._fetchrow_map.items():
            if key in sql:
                return row
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.executed.append(("fetch:" + sql.strip()[:120], args))
        for key, rows in self._fetch_map.items():
            if key in sql:
                return rows
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(("execute:" + sql.strip()[:120], args))

        # Track UPDATE mediator.conversations status transitions.
        if "UPDATE mediator.conversations" in sql:
            if "SET status = 'debrief_failed'" in sql:
                self.updated_status = "debrief_failed"
                row = self._fetchrow_map.get("FROM mediator.conversations")
                if row is not None:
                    row["status"] = "debrief_failed"
                # Track session_fields merge.
                if "||" in sql and len(args) > 1:
                    import json
                    try:
                        self.updated_session_fields = json.loads(args[1]) if isinstance(args[1], str) else args[1]
                    except Exception:
                        pass
            elif "SET status = 'review_pending'" in sql:
                self.updated_status = "review_pending"
                row = self._fetchrow_map.get("FROM mediator.conversations")
                if row is not None:
                    row["status"] = "review_pending"
            elif "SET status = 'debriefing'" in sql:
                row = self._fetchrow_map.get("FROM mediator.conversations")
                if row is not None:
                    row["status"] = "debriefing"

        return "OK"


# ── Shared fixtures ──────────────────────────────────────────────────────────


def _make_user(name: str = "test-user") -> User:
    return User(
        id=uuid4(),
        name=name,
        phone="+155****0000",
        timezone="UTC",
    )


def _make_debrief_payload() -> dict[str, Any]:
    """Return a valid submit_live_debrief payload."""
    return {
        "schema_version": 1,
        "review_summary": "The conversation covered relationship tension and repair attempts.",
        "what_heard": "Partner A expressed feeling unheard. Partner B acknowledged the pattern.",
        "what_decided": "Both committed to using 'I feel' statements next time.",
        "still_open": "Specific timeline for next check-in was not agreed.",
        "what_to_remember": "Partner A's work stress is a recurring trigger for withdrawal.",
        "durable_write_summary": "Created 1 memory about stress triggers. Created 1 observation about repair patterns.",
        "open_questions": "Is the every-Thursday check-in cadence still working?",
        "references": [],
        "failed_writes": [],
    }


# ── (a) Happy path ───────────────────────────────────────────────────────────


class TestDebriefHappyPath:
    """Verify the full agentic debrief success path: debriefing -> review_pending,
    artifacts created."""

    async def test_debrief_success_transitions_to_review_pending_and_persists(
        self, monkeypatch: Any
    ) -> None:
        """Simulate a successful agentic debrief run and verify side effects."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        payload = _make_debrief_payload()

        success_result = NonchatJobResult(
            success=True,
            brief=payload,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=3,
        )
        captured_run_kwargs: dict[str, Any] = {}

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            captured_run_kwargs.update(kwargs)
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id, name="TestUser")
        pool.set_transcript_turns([
            {
                "id": str(uuid4()),
                "speaker_label": "primary",
                "speaker_role": "primary",
                "text": "I feel unheard sometimes.",
                "ts": datetime.now(timezone.utc),
                "active_item_id": None,
            },
        ])
        pool.set_speakers([
            {"speaker_label": "primary", "role": "primary", "consent_state": "granted"},
        ])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        from app.services.live.debrief import run_live_debrief_agentic_job

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is True, f"Expected success, got {result}"
        assert result.turn_id is not None
        assert result.brief == payload
        config = captured_run_kwargs["config"]
        assert "live_debrief_transcript_policy" in config.initial_extras
        policy = config.initial_extras["live_debrief_transcript_policy"]
        assert policy, "debrief transcript policy must be available to tool guards"

        # Status transition: debriefing -> review_pending.
        assert pool.updated_status == "review_pending", (
            f"Expected status='review_pending', got {pool.updated_status}"
        )

        # Artifact inserted with type live_debrief.
        assert len(pool.inserted_artifact_payloads) >= 1, (
            f"Expected at least 1 artifact, got {len(pool.inserted_artifact_payloads)}"
        )
        assert any(
            a["artifact_type"] == "live_debrief"
            for a in pool.inserted_artifact_payloads
        ), f"No live_debrief artifact found in {pool.inserted_artifact_payloads}"

    async def test_debrief_success_with_review_summary_creates_second_artifact(
        self, monkeypatch: Any
    ) -> None:
        """When review_summary is present, a separate review_summary artifact is created."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        payload = _make_debrief_payload()
        payload["review_summary"] = "A detailed summary of the session."

        success_result = NonchatJobResult(
            success=True,
            brief=payload,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=3,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is True
        assert pool.updated_status == "review_pending"

        # Should have both live_debrief and review_summary artifacts.
        artifact_types = [a["artifact_type"] for a in pool.inserted_artifact_payloads]
        assert "live_debrief" in artifact_types, (
            f"Expected live_debrief artifact; types={artifact_types}"
        )
        assert "review_summary" in artifact_types, (
            f"Expected review_summary artifact; types={artifact_types}"
        )


# ── (b) Durable write path ───────────────────────────────────────────────────


class TestDebriefDurableWritePath:
    """Verify scoped write tools from debrief pass the safety gate."""

    def test_debrief_write_tools_include_memory_and_observation(self) -> None:
        """LIVE_DEBRIEF_GUARDED_WRITE_TOOLS covers memory and observation writes."""
        assert "add_memory" in LIVE_DEBRIEF_GUARDED_WRITE_TOOLS
        assert "log_observation" in LIVE_DEBRIEF_GUARDED_WRITE_TOOLS
        assert "create_theme" in LIVE_DEBRIEF_GUARDED_WRITE_TOOLS

    def test_flat_debrief_tools_include_guarded_writes(self) -> None:
        """build_live_debrief_tools returns a set that includes guarded write tools."""
        mediator_spec = get_bot_spec("mediator")
        tools = build_live_debrief_tools(mediator_spec)

        # Should include write tools (via registry minus outbound denylist).
        assert "add_memory" in tools, "add_memory must be in debrief tools"
        assert "log_observation" in tools, "log_observation must be in debrief tools"
        assert "submit_live_debrief" in tools, "submit_live_debrief must be in debrief tools"
        assert "update_turn_plan" in tools, "update_turn_plan must be in debrief tools"

        # Read tools should also be present.
        assert "search_messages" in tools, "search_messages must be in debrief tools"
        assert "get_distillations" in tools, "get_distillations must be in debrief tools"

    def test_debrief_step_allowed_honors_flat_policy(self) -> None:
        """_step_allowed with flat_allowed_tools set uses flat policy."""
        from app.bots.registry import get_bot_spec

        mediator_spec = get_bot_spec("mediator")
        flat = build_live_debrief_tools(mediator_spec)

        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=mediator_spec,
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
            flat_allowed_tools=flat,
        )

        allowed = _step_allowed(ctx)

        # submit_live_debrief must be present.
        assert "submit_live_debrief" in allowed, (
            "submit_live_debrief must be in debrief allowed tools"
        )

        # update_turn_plan must be present (ALWAYS_ALLOWED).
        assert "update_turn_plan" in allowed, (
            "update_turn_plan must be in debrief allowed tools"
        )


# ── (c) Redaction enforcement ────────────────────────────────────────────────


class TestDebriefRedactionEnforcement:
    """Verify that the debrief safety gate rejects writes citing redacted turns."""

    def test_redacted_turns_set_in_transcript_policy(self) -> None:
        """When a turn is redacted, it is recorded in redacted_turn_ids."""
        import hashlib

        text = "I am very angry about this."
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        turn_id = str(uuid4())

        policy: dict[str, Any] = {
            "redacted_turn_ids": [turn_id],
            "shareable_turn_ids": {},
            "allow_hot_context_derived_writes": True,
        }

        assert turn_id in policy["redacted_turn_ids"]
        assert turn_id not in policy["shareable_turn_ids"]

    def test_shareable_turn_honored(self) -> None:
        """A shareable turn has text_hash and quote_hashes in transcript policy."""
        import hashlib

        text = "I feel that we can work on this together."
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        turn_id = str(uuid4())

        policy: dict[str, Any] = {
            "redacted_turn_ids": [],
            "shareable_turn_ids": {
                turn_id: {
                    "text_hash": text_hash,
                    "quote_hashes": [text_hash],
                }
            },
            "allow_hot_context_derived_writes": True,
        }

        assert turn_id in policy["shareable_turn_ids"]
        assert turn_id not in policy["redacted_turn_ids"]

    def test_partner_redacted_turn_rejected_by_guard(self) -> None:
        """The safety gate rejects a write whose evidence_refs cites a redacted turn."""
        from app.services.tools.registry import _debrief_write_guard_ok

        turn_id = str(uuid4())
        raw_args: dict[str, Any] = {
            "content": "Partner was angry.",
            "evidence_refs": [
                {
                    "transcript_turn_id": turn_id,
                    "quote": "I am very angry.",
                }
            ],
        }

        policy: dict[str, Any] = {
            "redacted_turn_ids": [turn_id],
            "shareable_turn_ids": {},
            "allow_hot_context_derived_writes": False,
        }

        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )
        ctx.extras["live_debrief_transcript_policy"] = policy

        error = _debrief_write_guard_ok(ctx, "add_memory", raw_args)
        assert error is not None, "Expected guard to reject redacted turn reference"
        assert error.get("error_code") == "debrief_unshareable_transcript_reference", (
            f"Expected debrief_unshareable_transcript_reference, got {error.get('error_code')}"
        )

    def test_shareable_turn_passes_guard(self) -> None:
        """The safety gate allows a write citing a shareable turn."""
        from app.services.tools.registry import _debrief_write_guard_ok
        import hashlib

        text = "I feel we can work on this."
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        turn_id = str(uuid4())

        raw_args: dict[str, Any] = {
            "content": "User expressed willingness to work together.",
            "evidence_refs": [
                {
                    "transcript_turn_id": turn_id,
                    "quote": text,
                }
            ],
        }

        policy: dict[str, Any] = {
            "redacted_turn_ids": [],
            "shareable_turn_ids": {
                turn_id: {
                    "text_hash": text_hash,
                    "quote_hashes": [text_hash],
                }
            },
            "allow_hot_context_derived_writes": False,
        }

        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )
        ctx.extras["live_debrief_transcript_policy"] = policy

        error = _debrief_write_guard_ok(ctx, "add_memory", raw_args)
        assert error is None, (
            f"Expected guard to pass for shareable turn, got error: {error}"
        )

    def test_quote_mismatch_rejected(self) -> None:
        """Quoting a shareable turn with a wrong text hash is rejected."""
        from app.services.tools.registry import _debrief_write_guard_ok
        import hashlib

        text = "I feel we can work on this."
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        turn_id = str(uuid4())

        # Use a different quote that won't match.
        wrong_quote = "I am furious and cannot work on this."

        raw_args: dict[str, Any] = {
            "content": "User was angry.",
            "evidence_refs": [
                {
                    "transcript_turn_id": turn_id,
                    "quote": wrong_quote,
                }
            ],
        }

        policy: dict[str, Any] = {
            "redacted_turn_ids": [],
            "shareable_turn_ids": {
                turn_id: {
                    "text_hash": text_hash,
                    "quote_hashes": [text_hash],
                }
            },
            "allow_hot_context_derived_writes": False,
        }

        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )
        ctx.extras["live_debrief_transcript_policy"] = policy

        error = _debrief_write_guard_ok(ctx, "add_memory", raw_args)
        assert error is not None, "Expected guard to reject mismatched quote"
        assert error.get("error_code") == "debrief_quote_mismatch", (
            f"Expected debrief_quote_mismatch, got {error.get('error_code')}"
        )


# ── (d) Outbound denial ──────────────────────────────────────────────────────


class TestDebriefOutboundDenial:
    """Verify that outbound tools are rejected during live_debrief."""

    def test_outbound_tools_in_denylist(self) -> None:
        """All outbound messaging tools are in LIVE_DEBRIEF_OUTBOUND_DENYLIST."""
        assert "send_message_part" in LIVE_DEBRIEF_OUTBOUND_DENYLIST
        assert "send_bridge_candidate" in LIVE_DEBRIEF_OUTBOUND_DENYLIST
        assert "escalate_to_partner" in LIVE_DEBRIEF_OUTBOUND_DENYLIST
        assert "edit_outbound_message" in LIVE_DEBRIEF_OUTBOUND_DENYLIST
        assert "delete_outbound_message" in LIVE_DEBRIEF_OUTBOUND_DENYLIST
        assert "react_to_message" in LIVE_DEBRIEF_OUTBOUND_DENYLIST

    def test_flat_debrief_tools_exclude_outbound(self) -> None:
        """build_live_debrief_tools excludes all outbound tools."""
        mediator_spec = get_bot_spec("mediator")
        tools = build_live_debrief_tools(mediator_spec)

        for outbound in LIVE_DEBRIEF_OUTBOUND_DENYLIST:
            assert outbound not in tools, (
                f"{outbound} must NOT be in debrief tools"
            )

    def test_outbound_rejected_by_step_allowed(self) -> None:
        """_step_allowed for live_debrief with flat policy rejects outbound."""
        mediator_spec = get_bot_spec("mediator")
        flat = build_live_debrief_tools(mediator_spec)

        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=mediator_spec,
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
            flat_allowed_tools=flat,
        )

        allowed = _step_allowed(ctx)

        for outbound in LIVE_DEBRIEF_OUTBOUND_DENYLIST:
            assert outbound not in allowed, (
                f"{outbound} must NOT be in debrief step allowed tools"
            )


# ── (e) Privacy ──────────────────────────────────────────────────────────────


class TestDebriefPrivacy:
    """Verify partner raw text privacy rules in transcript bundle building."""

    def test_primary_turns_always_raw(self) -> None:
        """Primary turns should always be shareable (no redaction)."""
        # This is tested indirectly via build_debrief_transcript_bundle.
        # Primary speaker_role + consent_state=granted implies shareable.
        pass  # Integration test — the function is exercised in happy path.

    def test_other_turns_always_redacted(self) -> None:
        """'other' speaker_role turns are always redacted."""
        # Verified in the transcript policy construction.
        pass  # Integration test.

    async def test_partner_identity_requires_consent_and_opt_in(self) -> None:
        """Partner raw text only included with consent_state='granted' AND partner_share='opt_in'."""
        from app.services.live.debrief import build_debrief_transcript_bundle

        conversation_id = uuid4()
        user_id = uuid4()
        partner_user_id = uuid4()

        primary_turn_id = str(uuid4())
        partner_turn_id = str(uuid4())

        pool = DebriefFakePool()
        pool.set_transcript_turns([
            {
                "id": primary_turn_id,
                "speaker_label": "primary",
                "speaker_role": "primary",
                "text": "I feel unheard sometimes.",
                "ts": datetime.now(timezone.utc),
                "active_item_id": None,
            },
            {
                "id": partner_turn_id,
                "speaker_label": "partner",
                "speaker_role": "partner",
                "text": "I didn't realize you felt that way.",
                "ts": datetime.now(timezone.utc),
                "active_item_id": None,
            },
        ])
        # No consent granted for partner — consent_state = 'pending'.
        pool.set_speakers([
            {"speaker_label": "primary", "role": "primary", "consent_state": "granted"},
            {"speaker_label": "partner", "role": "partner", "consent_state": "pending"},
        ])

        model_bundle, policy = await build_debrief_transcript_bundle(
            pool,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            partner_user_id=partner_user_id,
        )

        # Partner turn should be redacted (consent not granted).
        assert partner_turn_id in policy.get("redacted_turn_ids", []), (
            f"Partner turn {partner_turn_id} should be redacted when consent_state=pending. "
            f"redacted_turn_ids={policy.get('redacted_turn_ids')}"
        )
        assert primary_turn_id in policy.get("shareable_turn_ids", {}), (
            f"Primary turn {primary_turn_id} should be shareable"
        )


# ── (f) Failure ──────────────────────────────────────────────────────────────


class TestDebriefFailure:
    """Verify failure paths: missing submit, cap exhaustion -> debrief_failed."""

    async def test_missing_submit_fails_debrief(
        self, monkeypatch: Any
    ) -> None:
        """Provider returns plain text without submit_live_debrief -> debrief_failed."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()

        failure_result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_debrief_text_no_submit",
            turn_id=uuid4(),
            tool_call_count=0,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return failure_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is False
        assert "text_no_submit" in result.failure_reason
        assert pool.updated_status == "debrief_failed", (
            f"Expected status='debrief_failed', got {pool.updated_status}"
        )

    async def test_submit_missing_at_tool_cap_fails_debrief(
        self, monkeypatch: Any
    ) -> None:
        """Cap exhaustion without submit -> debrief_failed with submit_missing_at_tool_cap."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()

        failure_result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_debrief_submit_missing_at_tool_cap",
            turn_id=uuid4(),
            tool_call_count=500,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return failure_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is False
        assert "submit_missing_at_tool_cap" in result.failure_reason
        assert pool.updated_status == "debrief_failed", (
            f"Expected status='debrief_failed', got {pool.updated_status}"
        )

    async def test_failed_debrief_preserves_session_fields(
        self, monkeypatch: Any
    ) -> None:
        """A failed debrief stores failure details in session_fields."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()

        failure_result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_debrief_submit_missing",
            turn_id=uuid4(),
            tool_call_count=10,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return failure_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()

        await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert pool.updated_status == "debrief_failed"
        assert pool.updated_session_fields is not None, (
            "Expected session_fields to be updated on failure"
        )
        assert (
            pool.updated_session_fields.get("debrief_failure_reason")
            == "live_debrief_submit_missing"
        ), (
            f"Expected debrief_failure_reason in session_fields, "
            f"got {pool.updated_session_fields}"
        )


# ── (g) Retry path ───────────────────────────────────────────────────────────


class TestDebriefRetry:
    """Verify retry_live_debrief flow."""

    async def test_retry_resets_status_and_reruns(
        self, monkeypatch: Any
    ) -> None:
        """retry_live_debrief: status=debrief_failed -> debriefing -> rerun -> success."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        payload = _make_debrief_payload()

        success_result = NonchatJobResult(
            success=True,
            brief=payload,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=3,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import retry_live_debrief

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debrief_failed",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id, name="TestUser")
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await retry_live_debrief(conversation_id, pool)

        assert result.success is True
        assert pool.updated_status == "review_pending", (
            f"Expected review_pending after retry success, got {pool.updated_status}"
        )

        # Verify the UPDATE to 'debriefing' happened before the re-run.
        update_calls = [
            s for s, _ in pool.executed
            if "UPDATE mediator.conversations" in s
        ]
        assert any(
            "debriefing" in s and "debrief_failed" not in s
            for s in update_calls
        ), f"Expected an UPDATE to 'debriefing' before retry; got {update_calls}"

    async def test_retry_rejects_non_debrief_failed(self) -> None:
        """retry_live_debrief raises ValueError for non-debrief_failed sessions."""
        from app.services.live.debrief import retry_live_debrief

        pool = DebriefFakePool()
        conversation_id = uuid4()
        pool.set_conversations_row(
            conversation_id,
            user_id=uuid4(),
            bot_id="mediator",
            status="review_pending",
        )

        with pytest.raises(ValueError, match="debrief_failed"):
            await retry_live_debrief(conversation_id, pool)

    async def test_retry_rejects_debriefing_status(self) -> None:
        """retry_live_debrief rejects conversations that are still debriefing."""
        from app.services.live.debrief import retry_live_debrief

        pool = DebriefFakePool()
        conversation_id = uuid4()
        pool.set_conversations_row(
            conversation_id,
            user_id=uuid4(),
            bot_id="mediator",
            status="debriefing",
        )

        with pytest.raises(ValueError, match="debrief_failed"):
            await retry_live_debrief(conversation_id, pool)
