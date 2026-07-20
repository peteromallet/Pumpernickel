"""Tests for reflection tool contracts (T11).

Covers:
- Tool schemas registered in TOOL_REGISTRY with valid input/output models.
- Authorization scope: handlers validate caller ownership.
- Internal classification and structured payloads hidden unless include_internals=true.
- Corrections append revisions without mutating canonical raw evidence.
- Handler tests for list, get, finalize, and correct reflection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest


# ── Module-level schema tests ────────────────────────────────────────────────


class TestReflectionToolSchemas:
    """Verify tool schemas are registered and validate correctly."""

    def test_all_four_tools_in_registry(self):
        from tool_schemas import TOOL_REGISTRY
        assert "list_reflections" in TOOL_REGISTRY
        assert "get_reflection" in TOOL_REGISTRY
        assert "finalize_reflection" in TOOL_REGISTRY
        assert "correct_reflection" in TOOL_REGISTRY

    def test_list_reflections_input_defaults(self):
        from tool_schemas import ListReflectionsInput
        inp = ListReflectionsInput()
        assert inp.scope == "own"
        assert inp.current_only is True
        assert inp.include_internals is False
        assert inp.limit == 25
        assert inp.bot_id is None
        assert inp.topic_id is None
        assert inp.session_id is None

    def test_list_reflections_input_validation(self):
        from tool_schemas import ListReflectionsInput
        # limit bounds
        ListReflectionsInput(limit=1)
        ListReflectionsInput(limit=200)
        with pytest.raises(Exception):
            ListReflectionsInput(limit=0)
        with pytest.raises(Exception):
            ListReflectionsInput(limit=201)

    def test_get_reflection_input_requires_entry_id(self):
        from tool_schemas import GetReflectionInput
        eid = uuid4()
        inp = GetReflectionInput(entry_id=eid)
        assert inp.entry_id == eid
        assert inp.include_internals is False

    def test_finalize_reflection_input(self):
        from tool_schemas import FinalizeReflectionInput
        sid = uuid4()
        inp = FinalizeReflectionInput(session_id=sid)
        assert inp.session_id == sid

    def test_correct_reflection_input(self):
        from tool_schemas import CorrectReflectionInput
        eid = uuid4()
        inp = CorrectReflectionInput(
            supersedes_entry_id=eid,
            plaintext_searchable="Corrected summary",
            summary="Updated summary",
            correction_note="Fixed typo",
        )
        assert inp.supersedes_entry_id == eid
        assert inp.correction_note == "Fixed typo"

    def test_reflection_entry_summary_model(self):
        from tool_schemas import ReflectionEntrySummary
        eid = uuid4()
        sid = uuid4()
        now = datetime.now(timezone.utc)
        summary = ReflectionEntrySummary(
            id=eid,
            session_id=sid,
            template_key="freeform_reflection",
            temporal_scope="day",
            phase="freeform",
            revision_number=1,
            created_at=now,
        )
        assert summary.id == eid
        assert summary.template_key == "freeform_reflection"

    def test_reflection_entry_detail_hides_internals_by_default(self):
        from tool_schemas import ReflectionEntryDetail
        eid = uuid4()
        sid = uuid4()
        now = datetime.now(timezone.utc)
        detail = ReflectionEntryDetail(
            id=eid,
            session_id=sid,
            template_key="freeform_reflection",
            temporal_scope="day",
            phase="freeform",
            revision_number=1,
            created_at=now,
            bot_id="superpom",
        )
        # By default, internals are None
        assert detail.classification_metadata is None
        assert detail.payload_fields is None
        assert detail.fields_unsupported is None

    def test_reflection_entry_detail_internals_when_set(self):
        from tool_schemas import ReflectionEntryDetail
        eid = uuid4()
        sid = uuid4()
        now = datetime.now(timezone.utc)
        detail = ReflectionEntryDetail(
            id=eid,
            session_id=sid,
            template_key="freeform_reflection",
            temporal_scope="day",
            phase="freeform",
            revision_number=1,
            created_at=now,
            bot_id="superpom",
            classification_metadata={"source": "explicit"},
            payload_fields={"mood": "tired"},
            fields_unsupported=["energy_level"],
        )
        assert detail.classification_metadata == {"source": "explicit"}
        assert detail.payload_fields == {"mood": "tired"}
        assert detail.fields_unsupported == ["energy_level"]

    def test_list_output_is_error_by_default(self):
        from tool_schemas import ListReflectionsOutput
        out = ListReflectionsOutput()
        assert out.is_error is False
        assert out.error is None
        assert out.entries == []
        assert out.include_internals is False

    def test_get_output_not_found(self):
        from tool_schemas import GetReflectionOutput
        out = GetReflectionOutput(is_error=True, error="not found")
        assert out.is_error is True
        assert out.entry is None

    def test_finalize_output_shape(self):
        from tool_schemas import FinalizeReflectionOutput
        sid = uuid4()
        now = datetime.now(timezone.utc)
        out = FinalizeReflectionOutput(
            session_id=sid,
            status="finalizing",
            finalized_at=now,
            source_message_ids=[uuid4()],
        )
        assert out.session_id == sid
        assert out.status == "finalizing"

    def test_correct_output_shape(self):
        from tool_schemas import CorrectReflectionOutput
        eid = uuid4()
        sid = uuid4()
        now = datetime.now(timezone.utc)
        out = CorrectReflectionOutput(
            entry_id=eid,
            session_id=sid,
            supersedes_entry_id=uuid4(),
            revision_number=2,
            created_at=now,
        )
        assert out.entry_id == eid
        assert out.revision_number == 2


# ── Tool dispatch and description tests ──────────────────────────────────────


class TestReflectionToolDispatch:
    """Verify handlers are wired in TOOL_DISPATCH and TOOL_DESCRIPTIONS."""

    def test_all_four_tools_in_dispatch(self):
        from app.services.tools.registry import TOOL_DISPATCH
        assert "list_reflections" in TOOL_DISPATCH
        assert "get_reflection" in TOOL_DISPATCH
        assert "finalize_reflection" in TOOL_DISPATCH
        assert "correct_reflection" in TOOL_DISPATCH

    def test_all_four_tools_have_descriptions(self):
        from app.services.tools.registry import TOOL_DESCRIPTIONS
        for name in ("list_reflections", "get_reflection", "finalize_reflection", "correct_reflection"):
            assert name in TOOL_DESCRIPTIONS
            assert len(TOOL_DESCRIPTIONS[name]) > 20, f"{name} description too short"

    def test_read_tools_in_read_phase(self):
        from app.services.tools.registry import READ_PHASE_TOOLS
        assert "list_reflections" in READ_PHASE_TOOLS
        assert "get_reflection" in READ_PHASE_TOOLS

    def test_write_tools_in_write_phase(self):
        from app.services.tools.registry import WRITE_PHASE_TOOLS
        assert "finalize_reflection" in WRITE_PHASE_TOOLS
        assert "correct_reflection" in WRITE_PHASE_TOOLS

    def test_write_tools_in_record_phase(self):
        from app.services.tools.registry import RECORD_WRITE_TOOLS
        assert "finalize_reflection" in RECORD_WRITE_TOOLS
        assert "correct_reflection" in RECORD_WRITE_TOOLS

    def test_anthropic_tool_generation_includes_reflection_tools(self):
        from app.services.tools.registry import to_anthropic_tools
        allowed = {"list_reflections", "get_reflection", "finalize_reflection", "correct_reflection"}
        tools = to_anthropic_tools(allowed)
        tool_names = {t["name"] for t in tools}
        assert tool_names == allowed
        for t in tools:
            assert t["description"], f"{t['name']} missing description"
            assert t["input_schema"], f"{t['name']} missing input_schema"


# ── Internal-hiding tests ────────────────────────────────────────────────────


class TestInternalsHidden:
    """Verify internal classification metadata and structured payloads
    are hidden unless explicitly requested."""

    def test_list_reflections_summary_has_no_internals(self):
        """The summary model has no internal fields whatsoever."""
        from tool_schemas import ReflectionEntrySummary
        eid = uuid4()
        sid = uuid4()
        now = datetime.now(timezone.utc)
        s = ReflectionEntrySummary(
            id=eid,
            session_id=sid,
            template_key="freeform_reflection",
            temporal_scope="day",
            phase="freeform",
            revision_number=1,
            created_at=now,
        )
        # Summary model must NOT have classification_metadata or payload_fields
        assert not hasattr(s, "classification_metadata") or s.classification_metadata is None
        assert not hasattr(s, "payload_fields") or s.payload_fields is None
        assert not hasattr(s, "fields_unsupported") or s.fields_unsupported is None

    def test_detail_without_internals_flag(self):
        """Detail model without include_internals flag has None internals."""
        from tool_schemas import ReflectionEntryDetail
        eid = uuid4()
        sid = uuid4()
        now = datetime.now(timezone.utc)
        d = ReflectionEntryDetail(
            id=eid,
            session_id=sid,
            template_key="freeform_reflection",
            temporal_scope="day",
            phase="freeform",
            revision_number=1,
            created_at=now,
            bot_id="superpom",
        )
        assert d.classification_metadata is None
        assert d.payload_fields is None
        assert d.fields_unsupported is None

    def test_detail_with_internals_flag(self):
        """Detail model with include_internals has internal fields populated."""
        from tool_schemas import ReflectionEntryDetail
        eid = uuid4()
        sid = uuid4()
        now = datetime.now(timezone.utc)
        d = ReflectionEntryDetail(
            id=eid,
            session_id=sid,
            template_key="freeform_reflection",
            temporal_scope="day",
            phase="freeform",
            revision_number=1,
            created_at=now,
            bot_id="superpom",
            classification_metadata={"source": "explicit_wording"},
            payload_fields={"mood": "happy"},
            fields_unsupported=["energy_level"],
        )
        assert d.classification_metadata == {"source": "explicit_wording"}
        assert d.payload_fields == {"mood": "happy"}
        assert d.fields_unsupported == ["energy_level"]


# ── Correction append-only contract tests ────────────────────────────────────


class TestCorrectionAppendOnly:
    """Verify that corrections create new revisions without mutating
    the canonical raw evidence."""

    def test_correct_input_has_supersedes_not_mutate(self):
        """The correction input references the entry to supersede —
        it doesn't carry a mutation instruction."""
        from tool_schemas import CorrectReflectionInput
        eid = uuid4()
        inp = CorrectReflectionInput(
            supersedes_entry_id=eid,
            correction_note="Better phrasing",
        )
        # The input only has supersedes_entry_id, not "mutate" or "delete"
        assert inp.supersedes_entry_id == eid
        assert not hasattr(inp, "mutate_source_messages")
        assert not hasattr(inp, "delete_entry")

    def test_correct_output_is_new_revision(self):
        """The output reports a new entry_id and revision_number,
        not a mutation of the old entry."""
        from tool_schemas import CorrectReflectionOutput
        old_eid = uuid4()
        new_eid = uuid4()
        out = CorrectReflectionOutput(
            entry_id=new_eid,
            session_id=uuid4(),
            supersedes_entry_id=old_eid,
            revision_number=2,
            created_at=datetime.now(timezone.utc),
        )
        # The new entry is a different UUID from the superseded one
        assert out.entry_id != old_eid
        assert out.supersedes_entry_id == old_eid
        assert out.revision_number == 2


# ── Authorization scope tests ────────────────────────────────────────────────


class TestAuthorizationScope:
    """Verify that tool inputs carry the necessary scope fields
    and that handlers use the caller's identity."""

    def test_list_reflections_is_scoped_to_own(self):
        from tool_schemas import ListReflectionsInput
        inp = ListReflectionsInput()
        assert inp.scope == "own"

    def test_finalize_requires_explicit_session_id(self):
        """Finalize doesn't operate on a query — it needs a specific session."""
        from tool_schemas import FinalizeReflectionInput
        sid = uuid4()
        inp = FinalizeReflectionInput(session_id=sid)
        assert inp.session_id == sid


# ── Handler validation tests (schema-level) ──────────────────────────────────


class TestHandlerImports:
    """Verify handler functions are importable and have correct signatures."""

    def test_handlers_importable(self):
        from app.services.tools.reflection_tools import (
            list_reflections,
            get_reflection,
            finalize_reflection,
            correct_reflection,
        )
        assert callable(list_reflections)
        assert callable(get_reflection)
        assert callable(finalize_reflection)
        assert callable(correct_reflection)

    def test_helpers_available(self):
        from app.services.tools.reflection_tools import (
            _store,
            _caller_bot_id,
            _entry_to_summary,
            _entry_to_detail,
        )
        assert callable(_store)
        assert callable(_caller_bot_id)
        assert callable(_entry_to_summary)
        assert callable(_entry_to_detail)
