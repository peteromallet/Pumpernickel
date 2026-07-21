"""Focused redaction tests for reflection-adjacent diagnostics.

Covers:
- Module-level constructs (exports, constants, safe/sensitive patterns)
- redact_reflection_diagnostics() — safe preservation, sensitive redaction,
  nested recursion, safe_extra / sensitive_extra, long-string heuristic
- redact_exception_message() — safe domain exceptions vs unknown exceptions
- redact_for_log_extra() — wrapper semantics
- Sentinel payload leak tests — confirm that known reflection payload
  sentinels do NOT appear in redacted output for each wired surface
  (log extra, exception messages, retry diagnostics, admin serialization,
  metric labels, eval output, release evidence)

These tests implement the T5 redaction gate: they must fail if sentinel
reflection payload text leaks through any output pathway tested here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.services.reflection_redaction import (
    REDACTED,
    redact_exception_message,
    redact_for_log_extra,
    redact_reflection_diagnostics,
)

# ── Sentinel payload text ────────────────────────────────────────────────────
# These strings simulate sensitive reflection content that MUST be suppressed
# by the redaction helpers.  Any test that finds one of these in redacted output
# is a FAILURE (security leak).

SENTINEL_CANONICAL = (
    "The user expressed frustration about recurring sleep issues and "
    "disclosed that they have been experiencing panic attacks before "
    "important work meetings. They mentioned their therapist's name "
    "and described a confidential medical diagnosis received last month."
)

SENTINEL_PAYLOAD = json.dumps({
    "mood": "anxious",
    "private_notes": "discussed marital tensions and financial stress",
    "action_items": ["call therapist", "meditate", "journal"],
})

SENTINEL_SUMMARY = (
    "Weekly reflection covering therapy progress, relationship challenges, "
    "and career uncertainty with specific references to a pending divorce "
    "filing and a job interview at a competitor company."
)

SENTINEL_TRANSCRIPT = (
    "Voice transcript from session on 2026-07-15: USER: I'm not sleeping well. "
    "BOT: Can you tell me more? USER: It's the same thing, my manager keeps "
    "micromanaging and I had another argument with my spouse about the kids."
)

SENTINEL_SEARCH_QUERY = "find all reflections where I discussed my therapy sessions and medication side effects"

SENTINEL_CORRECTION = (
    "Correcting the prior entry: I was wrong about the timeline. The incident "
    "actually occurred on June 12th, not July 3rd, and involved my colleague "
    "Sarah from the legal department, not Mark from engineering."
)


# ── Module-level constructs ───────────────────────────────────────────────────


class TestModuleExports:
    """Verify the redaction module exports the expected public API."""

    def test_public_functions_are_importable(self) -> None:
        assert callable(redact_reflection_diagnostics)
        assert callable(redact_exception_message)
        assert callable(redact_for_log_extra)

    def test_redacted_constant_is_string(self) -> None:
        assert isinstance(REDACTED, str)
        assert len(REDACTED) > 0


# ── redact_reflection_diagnostics ────────────────────────────────────────────


class TestRedactReflectionDiagnostics:
    """Core behaviour of the primary redaction function."""

    # ── basic input handling ───────────────────────────────────────────

    def test_none_returns_empty_dict(self) -> None:
        assert redact_reflection_diagnostics(None) == {}

    def test_empty_dict_returns_empty_dict(self) -> None:
        assert redact_reflection_diagnostics({}) == {}

    def test_unknown_shallow_keys_passthrough(self) -> None:
        result = redact_reflection_diagnostics({"custom_field": "hello"})
        assert result["custom_field"] == "hello"

    # ── safe field preservation ────────────────────────────────────────

    def test_opaque_ids_preserved(self) -> None:
        uid = uuid4()
        data = {
            "id": uid,
            "entry_id": uid,
            "session_id": uid,
            "user_id": uid,
            "bot_id": str(uid),
            "topic_id": uid,
            "supersedes_entry_id": uid,
            "created_by_turn_id": uid,
            "opened_by_message_id": uid,
            "opened_by_turn_id": uid,
            "source_id": uid,
            "message_id": uid,
            "dyad_id": uid,
            "claimed_by": "worker-1",
            "worker_id": "worker-1",
            "processor_version": 2,
            "idempotency_key": "key-abc",
        }
        result = redact_reflection_diagnostics(data)
        for key, expected in data.items():
            assert result[key] == expected, f"Safe ID field '{key}' was modified"

    def test_scope_keys_preserved(self) -> None:
        data = {
            "bot": "superpom",
            "user": "test-user",
            "topic": "topic-1",
            "session": "session-1",
            "_user": "test-user",
            "_bot": "superpom",
            "_session": "session-1",
            "_topic": "topic-1",
        }
        result = redact_reflection_diagnostics(data)
        for key, expected in data.items():
            assert result[key] == expected, f"Scope key '{key}' was modified"

    def test_state_keys_preserved(self) -> None:
        data = {
            "status": "processed",
            "session_status": "collecting",
            "state": "active",
            "phase": "retrospective",
            "action": "finalize",
            "decision": "finalize",
        }
        result = redact_reflection_diagnostics(data)
        for key, expected in data.items():
            assert result[key] == expected

    def test_reason_codes_preserved(self) -> None:
        data = {
            "reason": "idle_timeout",
            "failure_class": "retryable_processor",
            "failure_reason": "timeout",
            "last_error": "ConnectionError",
            "error_code": "E001",
            "classification_source": "classifier_v1",
        }
        result = redact_reflection_diagnostics(data)
        for key, expected in data.items():
            assert result[key] == expected

    def test_counts_preserved(self) -> None:
        data = {
            "revision_number": 3,
            "schema_version": 2,
            "retry_count": 1,
            "total_matched": 42,
            "scanned": 10,
            "finalized": 8,
            "abandoned": 1,
            "skipped_active": 0,
            "skipped_idempotent": 1,
            "errors": 0,
            "limit": 50,
            "lookback_days": 30,
            "max_entries": 100,
        }
        result = redact_reflection_diagnostics(data)
        for key, expected in data.items():
            assert result[key] == expected

    def test_timestamps_preserved(self) -> None:
        now = datetime.now(timezone.utc)
        data = {
            "opened_at": now,
            "finalized_at": now,
            "processed_at": now,
            "abandoned_at": now,
            "created_at": now,
            "updated_at": now,
            "sent_at": now,
            "source_created_at": now,
            "source_updated_at": now,
            "period_start": now,
            "period_end": now,
            "idle_finalize_at": now,
        }
        result = redact_reflection_diagnostics(data)
        for key, expected in data.items():
            assert result[key] == expected

    def test_flags_preserved(self) -> None:
        data = {
            "include_internals": True,
            "current_only": False,
            "is_open_loop": False,
            "is_error": True,
            "match_type": "semantic",
            "mode": "hybrid",
            "temporal_scope": "week",
            "timezone": "America/New_York",
            "template_key": "freeform",
            "compass_enabled": True,
            "allow_cross_topic_peek": False,
        }
        result = redact_reflection_diagnostics(data)
        for key, expected in data.items():
            assert result[key] == expected

    def test_scores_preserved(self) -> None:
        data = {
            "rrf_score": 0.85,
            "keyword_score": 0.72,
            "keyword_rank": 3,
            "semantic_rank": 1,
            "semantic_degraded": False,
            "classification_confidence": 0.95,
        }
        result = redact_reflection_diagnostics(data)
        for key, expected in data.items():
            assert result[key] == expected

    def test_structural_metadata_preserved(self) -> None:
        data = {
            "source_type": "message",
            "source_message_ids": [str(uuid4()), str(uuid4())],
            "_reflection_source_message_ids": [str(uuid4())],
            "_reflection_evidence": {"turn_count": 5},
            "evidence_metadata": {"source": "chat"},
            "classification_metadata": {"model": "v2"},
            "payload_fields": ["mood", "private_notes"],
            "fields_unsupported": [],
        }
        result = redact_reflection_diagnostics(data)
        for key, expected in data.items():
            assert result[key] == expected

    # ── sensitive field redaction ──────────────────────────────────────

    def test_plaintext_searchable_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "plaintext_searchable": SENTINEL_CANONICAL,
        })
        assert result["plaintext_searchable"] == REDACTED

    def test_canonical_text_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "canonical_text": SENTINEL_CANONICAL,
        })
        assert result["canonical_text"] == REDACTED

    def test_source_text_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "source_text": "Original message body here",
        })
        assert result["source_text"] == REDACTED

    def test_payload_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "payload": SENTINEL_PAYLOAD,
        })
        assert result["payload"] == REDACTED

    def test_summary_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "summary": SENTINEL_SUMMARY,
        })
        assert result["summary"] == REDACTED

    def test_summary_encrypted_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "summary_encrypted": "base64encryptedblob...",
        })
        assert result["summary_encrypted"] == REDACTED

    def test_correction_note_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "correction_note": SENTINEL_CORRECTION,
        })
        assert result["correction_note"] == REDACTED

    def test_query_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "query": SENTINEL_SEARCH_QUERY,
        })
        assert result["query"] == REDACTED

    def test_generic_text_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "text": "Some user-facing text content",
        })
        assert result["text"] == REDACTED

    def test_correction_payload_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "correction_payload": json.dumps({"correction": "fix timeline"}),
        })
        assert result["correction_payload"] == REDACTED

    def test_content_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "content": "Article body content here",
        })
        assert result["content"] == REDACTED

    def test_body_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "body": "Message body text",
        })
        assert result["body"] == REDACTED

    def test_transcript_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "transcript": SENTINEL_TRANSCRIPT,
        })
        assert result["transcript"] == REDACTED

    def test_decrypted_body_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "decrypted_body": "Decrypted message content",
        })
        assert result["decrypted_body"] == REDACTED

    def test_raw_message_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "raw_message": "Raw WhatsApp payload",
        })
        assert result["raw_message"] == REDACTED

    def test_searchable_content_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "searchable_content": SENTINEL_CANONICAL,
        })
        assert result["searchable_content"] == REDACTED

    # ── long-string heuristic ──────────────────────────────────────────

    def test_unknown_key_long_value_redacted(self) -> None:
        """Unknown keys with string values >200 chars are treated as payload."""
        long_text = "x" * 201  # 201 characters
        result = redact_reflection_diagnostics({"custom_notes": long_text})
        assert result["custom_notes"] == REDACTED

    def test_unknown_key_short_value_preserved(self) -> None:
        """Unknown keys with short values are passed through."""
        short_text = "x" * 200  # exactly 200 chars — not redacted
        result = redact_reflection_diagnostics({"custom_notes": short_text})
        assert result["custom_notes"] == short_text

    def test_error_key_long_value_preserved(self) -> None:
        """'error' key is exempt from long-string heuristic so error messages
        from known-safe domains aren't suppressed."""
        long_error = "E" * 300
        result = redact_reflection_diagnostics({"error": long_error})
        assert result["error"] == long_error

    def test_error_message_key_long_value_preserved(self) -> None:
        long_msg = "M" * 300
        result = redact_reflection_diagnostics({"error_message": long_msg})
        assert result["error_message"] == long_msg

    def test_message_key_long_value_preserved(self) -> None:
        long_msg = "M" * 300
        result = redact_reflection_diagnostics({"message": long_msg})
        assert result["message"] == long_msg

    def test_non_string_long_value_preserved(self) -> None:
        """Long values that aren't strings (lists, dicts) shouldn't be redacted
        by the length heuristic."""
        long_list = list(range(100))
        result = redact_reflection_diagnostics({"items": long_list})
        assert result["items"] == long_list

    # ── nested redaction ───────────────────────────────────────────────

    def test_nested_dict_sensitive_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "metadata": {
                "user_id": "abc",
                "canonical_text": SENTINEL_CANONICAL,
            },
        })
        assert result["metadata"]["user_id"] == "abc"
        assert result["metadata"]["canonical_text"] == REDACTED

    def test_nested_list_of_dicts_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "entries": [
                {"id": 1, "summary": SENTINEL_SUMMARY},
                {"id": 2, "summary": "Another summary"},
            ],
        })
        assert result["entries"][0]["id"] == 1
        assert result["entries"][0]["summary"] == REDACTED
        assert result["entries"][1]["id"] == 2
        assert result["entries"][1]["summary"] == REDACTED

    def test_deeply_nested_redacted(self) -> None:
        result = redact_reflection_diagnostics({
            "level1": {
                "level2": {
                    "level3": {
                        "canonical_text": SENTINEL_CANONICAL,
                        "id": "deep-id",
                    },
                },
            },
        })
        assert result["level1"]["level2"]["level3"]["canonical_text"] == REDACTED
        assert result["level1"]["level2"]["level3"]["id"] == "deep-id"

    # ── safe_extra / sensitive_extra ───────────────────────────────────

    def test_safe_extra_adds_fields(self) -> None:
        result = redact_reflection_diagnostics(
            {"custom_passthrough": "hello world"},
            safe_extra={"custom_passthrough"},
        )
        assert result["custom_passthrough"] == "hello world"

    def test_sensitive_extra_adds_fields(self) -> None:
        result = redact_reflection_diagnostics(
            {"custom_secret": "do not log this"},
            sensitive_extra={"custom_secret"},
        )
        assert result["custom_secret"] == REDACTED

    def test_sensitive_extra_overrides_safe(self) -> None:
        """sensitive_extra should take priority over safe patterns."""
        result = redact_reflection_diagnostics(
            {"custom_field": "secret"},
            safe_extra={"custom_field"},
            sensitive_extra={"custom_field"},
        )
        # sensitive takes priority because it's checked first in the loop
        assert result["custom_field"] == REDACTED

    # ── immutable input ────────────────────────────────────────────────

    def test_original_dict_not_mutated(self) -> None:
        original = {"canonical_text": SENTINEL_CANONICAL, "id": "keep-me"}
        _result = redact_reflection_diagnostics(original)
        assert original["canonical_text"] == SENTINEL_CANONICAL
        assert original["id"] == "keep-me"


# ── redact_exception_message ──────────────────────────────────────────────────


class TestRedactExceptionMessage:
    """Exception message redaction for safe-vs-unknown domain exceptions."""

    def test_safe_session_not_found_carries_message(self) -> None:
        from app.services.reflections import SessionNotFoundError
        exc = SessionNotFoundError("session 123e4567-e89b-12d3-a456-426614174000 not found")
        msg = redact_exception_message(exc)
        assert "SessionNotFoundError" in msg
        assert "123e4567" in msg  # UUID is safe

    def test_safe_session_not_collecting_carries_message(self) -> None:
        from app.services.reflections import SessionNotCollectingError
        exc = SessionNotCollectingError("session is not in collecting state")
        msg = redact_exception_message(exc)
        assert "SessionNotCollectingError" in msg
        assert "not in collecting" in msg

    def test_safe_session_not_finalizing_carries_message(self) -> None:
        from app.services.reflections import SessionNotFinalizingError
        exc = SessionNotFinalizingError("session is not finalizing")
        msg = redact_exception_message(exc)
        assert "SessionNotFinalizingError" in msg

    def test_safe_session_claim_conflict_carries_message(self) -> None:
        from app.services.reflections import SessionClaimConflictError
        exc = SessionClaimConflictError("session already claimed by worker-2")
        msg = redact_exception_message(exc)
        assert "SessionClaimConflictError" in msg

    def test_safe_session_finalize_conflict_carries_message(self) -> None:
        from app.services.reflections import SessionFinalizeConflictError
        exc = SessionFinalizeConflictError("session already finalized")
        msg = redact_exception_message(exc)
        assert "SessionFinalizeConflictError" in msg

    def test_safe_entry_not_found_carries_message(self) -> None:
        from app.services.reflections import EntryNotFoundError
        exc = EntryNotFoundError("entry not found")
        msg = redact_exception_message(exc)
        assert "EntryNotFoundError" in msg

    def test_safe_entry_revision_conflict_carries_message(self) -> None:
        from app.services.reflections import EntryRevisionConflictError
        exc = EntryRevisionConflictError("revision conflict")
        msg = redact_exception_message(exc)
        assert "EntryRevisionConflictError" in msg

    def test_safe_entry_correction_error_carries_message(self) -> None:
        from app.services.reflections import EntryCorrectionError
        exc = EntryCorrectionError("correction error")
        msg = redact_exception_message(exc)
        assert "EntryCorrectionError" in msg

    def test_safe_derivation_not_found_carries_message(self) -> None:
        from app.services.reflections import DerivationNotFoundError
        exc = DerivationNotFoundError("derivation not found")
        msg = redact_exception_message(exc)
        assert "DerivationNotFoundError" in msg

    def test_safe_derivation_idempotency_conflict_carries_message(self) -> None:
        from app.services.reflections import DerivationIdempotencyConflictError
        exc = DerivationIdempotencyConflictError("idempotency conflict")
        msg = redact_exception_message(exc)
        assert "DerivationIdempotencyConflictError" in msg

    def test_safe_derivation_decision_error_carries_message(self) -> None:
        from app.services.reflections import DerivationDecisionError
        exc = DerivationDecisionError("invalid decision")
        msg = redact_exception_message(exc)
        assert "DerivationDecisionError" in msg

    def test_unknown_exception_redacted(self) -> None:
        """Unknown exceptions (like ValueError, RuntimeError with user text)
        should be redacted to type name only."""
        exc = ValueError(SENTINEL_CANONICAL)
        msg = redact_exception_message(exc)
        assert "ValueError" in msg
        assert "(message redacted)" in msg
        assert "frustration" not in msg
        assert "panic attacks" not in msg
        assert "therapist" not in msg

    def test_generic_runtime_error_redacted(self) -> None:
        exc = RuntimeError(f"Failed to process: {SENTINEL_SUMMARY}")
        msg = redact_exception_message(exc)
        assert "RuntimeError" in msg
        assert "(message redacted)" in msg
        assert "therapy" not in msg
        assert "divorce" not in msg

    def test_exception_with_uuid_in_message_redacted(self) -> None:
        """Even if a ValueError contains a UUID, it's still an unknown type
        and should be fully redacted."""
        exc = ValueError(f"bad data in entry {uuid4()}: {SENTINEL_TRANSCRIPT}")
        msg = redact_exception_message(exc)
        assert "ValueError" in msg
        assert "(message redacted)" in msg
        assert "transcript" not in msg.lower()


# ── redact_for_log_extra ──────────────────────────────────────────────────────


class TestRedactForLogExtra:
    """Wrapper semantics for logger extra= dicts."""

    def test_delegates_to_redact_reflection_diagnostics(self) -> None:
        result = redact_for_log_extra({
            "user_id": "abc",
            "canonical_text": SENTINEL_CANONICAL,
        })
        assert result["user_id"] == "abc"
        assert result["canonical_text"] == REDACTED

    def test_none_returns_empty(self) -> None:
        assert redact_for_log_extra(None) == {}


# ── Sentinel payload leak tests (surface-level) ──────────────────────────────

# Each test below simulates one of the 11 wired diagnostic surfaces from T4.
# The test passes only if NO sentinel payload text appears in the redacted output.


class TestSentinelPayloadLeaks:
    """Confirm sentinel payload text is suppressed across all wired surfaces."""

    # ── helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _assert_no_sentinels(output: str | dict, *, label: str = "") -> None:
        """Assert that no sentinel payload text appears in *output*."""
        if isinstance(output, dict):
            serialized = json.dumps(output, default=str)
        else:
            serialized = str(output)

        sentinels = [
            ("SENTINEL_CANONICAL", SENTINEL_CANONICAL),
            ("SENTINEL_PAYLOAD", SENTINEL_PAYLOAD),
            ("SENTINEL_SUMMARY", SENTINEL_SUMMARY),
            ("SENTINEL_TRANSCRIPT", SENTINEL_TRANSCRIPT),
            ("SENTINEL_SEARCH_QUERY", SENTINEL_SEARCH_QUERY),
            ("SENTINEL_CORRECTION", SENTINEL_CORRECTION),
        ]

        context = f" [{label}]" if label else ""

        for name, sentinel in sentinels:
            # Check for substrings of the sentinel that would indicate a leak.
            # We use fragments that are distinctive enough to avoid false positives.
            fragments = _extract_distinctive_fragments(sentinel)
            for fragment in fragments:
                assert fragment not in serialized, (
                    f"SENTINEL LEAK{context}: fragment '{fragment}' from "
                    f"{name} found in redacted output"
                )


def _extract_distinctive_fragments(text: str, min_len: int = 15) -> list[str]:
    """Extract distinctive word-sequence fragments from sentinel text.

    Returns fragments long enough to be unambiguous leaks but short enough
    to catch partial exposures.
    """
    words = text.split()
    fragments: list[str] = []
    for i in range(len(words)):
        for j in range(i + 3, min(i + 8, len(words) + 1)):
            frag = " ".join(words[i:j])
            if len(frag) >= min_len:
                fragments.append(frag)
    # Deduplicate and limit to avoid overly large assertion sets
    seen: set[str] = set()
    unique: list[str] = []
    for f in fragments:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    # Return enough to catch leaks but not so many that tests are noisy
    return unique[:20]


# ── Wired surface simulation tests ────────────────────────────────────────────


class TestWiredSurfaceR1:
    """search_reflections retrieval failed (reflection_tools.py:317)."""

    def test_extra_dict_redacted(self) -> None:
        extra = {
            "user_id": str(uuid4()),
            "bot_id": "superpom",
            "topic_id": str(uuid4()),
            "mode": "hybrid",
            "query": SENTINEL_SEARCH_QUERY,  # should be redacted
        }
        result = redact_for_log_extra(extra)
        assert result["user_id"] == extra["user_id"]
        assert result["bot_id"] == extra["bot_id"]
        assert result["topic_id"] == extra["topic_id"]
        assert result["mode"] == extra["mode"]
        assert result["query"] == REDACTED
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R1")


class TestWiredSurfaceR2:
    """_reconcile_after_correction probe failure (reflection_tools.py:513)."""

    def test_extra_dict_redacted(self) -> None:
        extra = {
            "superseded_entry_id": str(uuid4()),
            "user_id": str(uuid4()),
        }
        result = redact_for_log_extra(extra)
        assert result["superseded_entry_id"] == extra["superseded_entry_id"]
        assert result["user_id"] == extra["user_id"]
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R2")


class TestWiredSurfaceR3:
    """_reconcile_after_correction reconciliation failed (reflection_tools.py:536)."""

    def test_extra_dict_redacted(self) -> None:
        extra = {
            "superseded_entry_id": str(uuid4()),
            "corrected_entry_id": str(uuid4()),
            "user_id": str(uuid4()),
        }
        result = redact_for_log_extra(extra)
        assert result["superseded_entry_id"] == extra["superseded_entry_id"]
        assert result["corrected_entry_id"] == extra["corrected_entry_id"]
        assert result["user_id"] == extra["user_id"]
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R3")


class TestWiredSurfaceR4:
    """correct_reflection failed (reflection_tools.py:607)."""

    def test_extra_dict_with_payload_sentinels_redacted(self) -> None:
        """Simulate an extra dict that accidentally includes sensitive fields."""
        extra = {
            "user_id": str(uuid4()),
            "supersedes_entry_id": str(uuid4()),
            "bot_id": "superpom",
            # These would be leaks if not redacted
            "canonical_text": SENTINEL_CANONICAL,
            "plaintext_searchable": SENTINEL_CANONICAL,
            "summary": SENTINEL_SUMMARY,
            "correction_note": SENTINEL_CORRECTION,
            "payload": SENTINEL_PAYLOAD,
        }
        result = redact_for_log_extra(extra)
        assert result["canonical_text"] == REDACTED
        assert result["plaintext_searchable"] == REDACTED
        assert result["summary"] == REDACTED
        assert result["correction_note"] == REDACTED
        assert result["payload"] == REDACTED
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R4")


class TestWiredSurfaceR5:
    """finalization worker tick failed (reflections_finalization_worker.py:116)."""

    def test_extra_dict_redacted(self) -> None:
        extra = {"worker_id": "worker-1"}
        result = redact_for_log_extra(extra)
        assert result["worker_id"] == "worker-1"
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R5")


class TestWiredSurfaceR6:
    """finalization worker session processing error (reflections_finalization_worker.py:188)."""

    def test_extra_dict_redacted(self) -> None:
        extra = {
            "worker_id": "worker-2",
            "session_id": str(uuid4()),
        }
        result = redact_for_log_extra(extra)
        assert result["worker_id"] == "worker-2"
        assert result["session_id"] == extra["session_id"]
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R6")


class TestWiredSurfaceR7:
    """finalization worker normalize+create_entry failed (reflections_finalization_worker.py:348)."""

    def test_extra_dict_redacted(self) -> None:
        extra = {
            "session_id": str(uuid4()),
            "user_id": str(uuid4()),
            "bot_id": "superpom",
            "worker_id": "worker-3",
        }
        result = redact_for_log_extra(extra)
        assert result["session_id"] == extra["session_id"]
        assert result["user_id"] == extra["user_id"]
        assert result["bot_id"] == extra["bot_id"]
        assert result["worker_id"] == extra["worker_id"]
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R7")


class TestWiredSurfaceR8:
    """mark_session_processed embed enqueue failed (reflections.py:1067)."""

    def test_extra_dict_redacted(self) -> None:
        extra = {"session_id": str(uuid4())}
        result = redact_for_log_extra(extra)
        assert result["session_id"] == extra["session_id"]
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R8")


class TestWiredSurfaceR9:
    """create_entry embed enqueue failed (reflections.py:1638)."""

    def test_extra_dict_redacted(self) -> None:
        extra = {
            "entry_id": str(uuid4()),
            "session_id": str(uuid4()),
        }
        result = redact_for_log_extra(extra)
        assert result["entry_id"] == extra["entry_id"]
        assert result["session_id"] == extra["session_id"]
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R9")


class TestWiredSurfaceR10:
    """correct_entry embed lifecycle failed (reflections.py:2012)."""

    def test_extra_dict_redacted(self) -> None:
        extra = {
            "entry_id": str(uuid4()),
            "supersedes_entry_id": str(uuid4()),
        }
        result = redact_for_log_extra(extra)
        assert result["entry_id"] == extra["entry_id"]
        assert result["supersedes_entry_id"] == extra["supersedes_entry_id"]
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R10")


class TestWiredSurfaceR11:
    """search_reflections query log (reflection_tools.py:289)
    — query is pre-redacted inline as '[REDACTED search query]'."""

    def test_query_field_redacted(self) -> None:
        extra = {
            "user_id": str(uuid4()),
            "bot_id": "superpom",
            "topic_id": str(uuid4()),
            "mode": "hybrid",
            "query": SENTINEL_SEARCH_QUERY,
        }
        result = redact_for_log_extra(extra)
        assert result["query"] == REDACTED
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="R11")


# ── Cross-cutting leak tests ──────────────────────────────────────────────────


class TestEvalOutputLeakPrevention:
    """Eval output surfaces must not leak sentinel payload text."""

    def test_eval_style_diagnostic_dict(self) -> None:
        """Simulate diagnostic dicts that could end up in eval output."""
        diagnostic = {
            "eval_run_id": str(uuid4()),
            "bot_id": "superpom",
            "user_id": str(uuid4()),
            "reflection_entry": {
                "id": str(uuid4()),
                "canonical_text": SENTINEL_CANONICAL,
                "summary": SENTINEL_SUMMARY,
                "payload": SENTINEL_PAYLOAD,
            },
            "status": "processed",
            "errors": 0,
        }
        result = redact_reflection_diagnostics(diagnostic)
        inner = result["reflection_entry"]
        assert inner["canonical_text"] == REDACTED
        assert inner["summary"] == REDACTED
        assert inner["payload"] == REDACTED
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="eval-output")


class TestAdminSerializationLeakPrevention:
    """Admin-facing serialization must not leak sentinel payload text."""

    def test_admin_style_row_dict(self) -> None:
        """Simulate a row dict that could be serialized for admin output."""
        row = {
            "id": str(uuid4()),
            "user_id": str(uuid4()),
            "bot_id": "superpom",
            "status": "processed",
            "retry_count": 0,
            "failure_class": None,
            "canonical_text": SENTINEL_CANONICAL,
            "plaintext_searchable": SENTINEL_CANONICAL,
            "summary": SENTINEL_SUMMARY,
            "payload": SENTINEL_PAYLOAD,
            "transcript": SENTINEL_TRANSCRIPT,
            "searchable_content": SENTINEL_CANONICAL,
            "created_at": datetime.now(timezone.utc),
        }
        result = redact_reflection_diagnostics(row)
        # Safe fields preserved
        assert result["id"] == row["id"]
        assert result["status"] == "processed"
        assert result["retry_count"] == 0
        # Sensitive fields redacted
        assert result["canonical_text"] == REDACTED
        assert result["plaintext_searchable"] == REDACTED
        assert result["summary"] == REDACTED
        assert result["payload"] == REDACTED
        assert result["transcript"] == REDACTED
        assert result["searchable_content"] == REDACTED
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="admin-row")


class TestRetryDiagnosticsLeakPrevention:
    """Retry diagnostics must not leak sentinel payload text."""

    def test_retry_diagnostic_dict(self) -> None:
        diagnostic = {
            "entry_id": str(uuid4()),
            "session_id": str(uuid4()),
            "retry_count": 2,
            "failure_class": "retryable_processor",
            "failure_reason": "timeout",
            "last_error": "ConnectionError",
            "canonical_text": SENTINEL_CANONICAL,
            "source_text": "Source message body",
            "payload": {"nested": "data"},
        }
        result = redact_reflection_diagnostics(diagnostic)
        assert result["entry_id"] == diagnostic["entry_id"]
        assert result["retry_count"] == 2
        assert result["failure_class"] == "retryable_processor"
        assert result["failure_reason"] == "timeout"
        assert result["last_error"] == "ConnectionError"
        assert result["canonical_text"] == REDACTED
        assert result["source_text"] == REDACTED
        assert result["payload"] == REDACTED
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="retry-diagnostics")


class TestMetricLabelLeakPrevention:
    """Metric labels must not carry sentinel payload text."""

    def test_metric_label_dict_redacted(self) -> None:
        labels = {
            "bot_id": "superpom",
            "status": "processed",
            "failure_class": "retryable_processor",
            "summary": SENTINEL_SUMMARY,  # should NOT appear in metric labels
            "query": SENTINEL_SEARCH_QUERY,
        }
        result = redact_reflection_diagnostics(labels)
        assert result["bot_id"] == "superpom"
        assert result["status"] == "processed"
        assert result["failure_class"] == "retryable_processor"
        assert result["summary"] == REDACTED
        assert result["query"] == REDACTED
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="metric-labels")


class TestReleaseEvidenceLeakPrevention:
    """Release evidence must not contain sentinel payload text."""

    def test_evidence_metadata_redacted(self) -> None:
        evidence = {
            "surface": "R1-search_reflections-retrieval-failed",
            "file": "reflection_tools.py",
            "line": 317,
            "status": "redacted",
            # Use actual sensitive field names that MUST be redacted
            "payload": SENTINEL_PAYLOAD,
            "canonical_text": SENTINEL_CANONICAL,
            "transcript": SENTINEL_TRANSCRIPT,
        }
        result = redact_reflection_diagnostics(evidence)
        assert result["surface"] == evidence["surface"]
        assert result["file"] == evidence["file"]
        assert result["line"] == 317
        assert result["status"] == "redacted"
        # These would be false-certified as safe if they leaked
        assert result["payload"] == REDACTED
        assert result["canonical_text"] == REDACTED
        assert result["transcript"] == REDACTED
        TestSentinelPayloadLeaks._assert_no_sentinels(result, label="release-evidence")
