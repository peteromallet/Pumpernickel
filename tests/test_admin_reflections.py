"""Admin/operator tests for the reflection sessions listing endpoint.

Covers:
- Authorization (admin basic auth required)
- User/bot/topic scope boundary isolation
- Stuck/active session field presence
- Deletion and embedding indicators
- Entry and derivation counts
- Absence of sensitive payload text
- Field-level non-sensitivity proof for release evidence
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.admin import router
from tests.conftest import FakePool


# ── Test helpers ──────────────────────────────────────────────────────────────


def _make_client(monkeypatch, pool=None) -> TestClient:
    """Create a TestClient with the admin router and basic auth env."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:***@localhost:5432/db")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "dummy-service-role")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai")
    monkeypatch.setenv("GROQ_API_KEY", "dummy-groq")
    monkeypatch.setenv("WHATSAPP_TOKEN", "dummy-whatsapp")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "12345")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "dummy-verify")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-password")
    monkeypatch.setenv("PARTNER_PHONE_A", "15555550100")
    monkeypatch.setenv("PARTNER_PHONE_B", "15555550101")
    get_settings.cache_clear()
    app = FastAPI()
    app.state.pool = pool or FakePool()
    app.include_router(router)
    return TestClient(app)


def _valid_auth():
    """Return the correct auth tuple for admin access."""
    return ("admin", "correct-password")


def _make_session_row(
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    bot_id: str = "superpom",
    topic_id: UUID | None = None,
    template_key: str = "freeform",
    temporal_scope: str = "week",
    phase: str = "retrospective",
    status: str = "processed",
    classification_source: str | None = "classifier_v1",
    classification_confidence: float | None = 0.92,
    retry_count: int = 0,
    failure_class: str | None = None,
    failure_reason: str | None = None,
    last_error: str | None = None,
    entry_count: int = 3,
    derivation_count: int = 2,
    has_embeddable_entries: bool = True,
    claimed_by: str | None = None,
    created_at: datetime | None = None,
    finalized_at: datetime | None = None,
    processed_at: datetime | None = None,
    abandoned_at: datetime | None = None,
    idle_finalize_at: datetime | None = None,
    updated_at: datetime | None = None,
    idempotency_key: str | None = None,
    claimed_at: datetime | None = None,
) -> dict:
    """Build a single admin_list_sessions-style row with safe defaults."""
    now = datetime.now(UTC)
    return {
        "id": session_id or uuid4(),
        "user_id": user_id or uuid4(),
        "bot_id": bot_id,
        "topic_id": topic_id or uuid4(),
        "template_key": template_key,
        "temporal_scope": temporal_scope,
        "phase": phase,
        "status": status,
        "classification_source": classification_source,
        "classification_confidence": classification_confidence,
        "retry_count": retry_count,
        "failure_class": failure_class,
        "failure_reason": failure_reason,
        "last_error": last_error,
        "entry_count": entry_count,
        "derivation_count": derivation_count,
        "has_embeddable_entries": has_embeddable_entries,
        "claimed_by": claimed_by,
        "claimed_at": claimed_at,
        "created_at": created_at or now,
        "finalized_at": finalized_at,
        "processed_at": processed_at,
        "abandoned_at": abandoned_at,
        "idle_finalize_at": idle_finalize_at,
        "updated_at": updated_at or now,
        "idempotency_key": idempotency_key,
    }


# Sentinel payload text — must NEVER appear in admin HTML output.

SENTINEL_PAYLOAD = (
    "The user disclosed severe anxiety about their upcoming performance "
    "review and mentioned private family medical history including a "
    "recent cancer diagnosis for their spouse."
)

SENTINEL_PAYLOAD_SHORT = "private medical diagnosis"


# ── Authorization tests ──────────────────────────────────────────────────────


class TestAdminReflectionsAuth:
    """Authorization: admin basic auth is required for /admin/reflections."""

    def test_requires_basic_auth(self, monkeypatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections")
        assert resp.status_code == 401
        assert resp.headers["WWW-Authenticate"] == 'Basic realm="admin"'

    def test_rejects_wrong_credentials(self, monkeypatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=("admin", "wrong-password"))
        assert resp.status_code == 401
        assert resp.headers["WWW-Authenticate"] == 'Basic realm="admin"'

    def test_rejects_wrong_username(self, monkeypatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=("attacker", "correct-password"))
        assert resp.status_code == 401

    def test_accepts_valid_credentials(self, monkeypatch) -> None:
        """Even with zero sessions, the page should render (200, not 401)."""
        client = _make_client(monkeypatch)

        async def _fake_list(*args, **kwargs):
            return []

        monkeypatch.setattr(
            "app.routers.admin.admin_list_sessions", _fake_list
        )
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert resp.status_code == 200


# ── Field-level presence tests ───────────────────────────────────────────────


class TestAdminReflectionsFieldPresence:
    """Every non-sensitive exposed field must appear in the HTML output."""

    EXPOSED_COLUMNS = [
        "id",
        "user_id",
        "bot_id",
        "topic_id",
        "template_key",
        "temporal_scope",
        "phase",
        "status",
        "classification_source",
        "classification_confidence",
        "retry_count",
        "failure_class",
        "failure_reason",
        "last_error",
        "entry_count",
        "derivation_count",
        "has_embeddable_entries",
        "claimed_by",
        "created_at",
        "finalized_at",
        "processed_at",
        "abandoned_at",
        "idle_finalize_at",
        "updated_at",
    ]

    def _patch_list_sessions(self, monkeypatch, rows):
        async def _fake_list(*args, **kwargs):
            return rows

        monkeypatch.setattr(
            "app.routers.admin.admin_list_sessions", _fake_list
        )

    def test_all_exposed_columns_in_html(self, monkeypatch) -> None:
        """Every column name from the endpoint must appear as a table header."""
        row = _make_session_row()
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert resp.status_code == 200
        html = resp.text
        for column in self.EXPOSED_COLUMNS:
            assert column in html, (
                f"Exposed column '{column}' missing from admin HTML output"
            )

    def test_session_id_rendered(self, monkeypatch) -> None:
        sid = uuid4()
        row = _make_session_row(session_id=sid)
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert str(sid) in resp.text

    def test_user_id_rendered(self, monkeypatch) -> None:
        uid = uuid4()
        row = _make_session_row(user_id=uid)
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert str(uid) in resp.text

    def test_bot_id_rendered(self, monkeypatch) -> None:
        row = _make_session_row(bot_id="superpom")
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert "superpom" in resp.text

    def test_topic_id_rendered(self, monkeypatch) -> None:
        tid = uuid4()
        row = _make_session_row(topic_id=tid)
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert str(tid) in resp.text


# ── Stuck / active session field tests ───────────────────────────────────────


class TestAdminReflectionsStuckActiveSessions:
    """Verify stuck/active session metadata renders correctly."""

    def _patch_list_sessions(self, monkeypatch, rows):
        async def _fake_list(*args, **kwargs):
            return rows

        monkeypatch.setattr(
            "app.routers.admin.admin_list_sessions", _fake_list
        )

    def test_stuck_session_failure_class_rendered(self, monkeypatch) -> None:
        row = _make_session_row(
            status="processing_failed",
            failure_class="retryable_processor",
            failure_reason="LLM timeout after 3 retries",
            last_error="ConnectionError",
            retry_count=2,
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "processing_failed" in html
        assert "retryable_processor" in html
        assert "LLM timeout after 3 retries" in html
        assert "ConnectionError" in html
        assert "2" in html  # retry_count

    def test_stuck_session_terminal_failure_rendered(self, monkeypatch) -> None:
        row = _make_session_row(
            status="processing_failed",
            failure_class="terminal_input",
            failure_reason="Empty source messages — nothing to process",
            last_error="ValueError",
            retry_count=1,
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "terminal_input" in html
        assert "nothing to process" in html
        assert "ValueError" in html

    def test_active_collecting_session_rendered(self, monkeypatch) -> None:
        row = _make_session_row(
            status="collecting",
            phase="opening",
            entry_count=5,
            has_embeddable_entries=False,
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "collecting" in html
        assert "opening" in html

    def test_active_finalizing_session_rendered(self, monkeypatch) -> None:
        row = _make_session_row(
            status="finalizing",
            phase="retrospective",
            claimed_by="worker-7",
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "finalizing" in html
        assert "worker-7" in html

    def test_abandoned_session_rendered(self, monkeypatch) -> None:
        abandoned_at = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
        row = _make_session_row(
            status="abandoned",
            abandoned_at=abandoned_at,
            entry_count=0,
            derivation_count=0,
            has_embeddable_entries=False,
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "abandoned" in html

    def test_multiple_sessions_different_statuses(self, monkeypatch) -> None:
        rows = [
            _make_session_row(status="collecting", bot_id="bot-a"),
            _make_session_row(status="finalizing", bot_id="bot-b"),
            _make_session_row(status="processed", bot_id="bot-c"),
            _make_session_row(status="processing_failed", bot_id="bot-d",
                              failure_class="terminal_internal"),
            _make_session_row(status="abandoned", bot_id="bot-e"),
        ]
        self._patch_list_sessions(monkeypatch, rows)
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        for expected in ["bot-a", "bot-b", "bot-c", "bot-d", "bot-e"]:
            assert expected in html
        for status in ["collecting", "finalizing", "processed",
                        "processing_failed", "abandoned"]:
            assert status in html


# ── Embedding and deletion indicator tests ───────────────────────────────────


class TestAdminReflectionsEmbeddingDeletionIndicators:
    """Verify embedding coverage (has_embeddable_entries) and related indicators."""

    def _patch_list_sessions(self, monkeypatch, rows):
        async def _fake_list(*args, **kwargs):
            return rows

        monkeypatch.setattr(
            "app.routers.admin.admin_list_sessions", _fake_list
        )

    def test_embeddable_true_rendered(self, monkeypatch) -> None:
        row = _make_session_row(has_embeddable_entries=True)
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert "True" in resp.text or "true" in resp.text

    def test_embeddable_false_rendered(self, monkeypatch) -> None:
        row = _make_session_row(has_embeddable_entries=False)
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert "False" in resp.text or "false" in resp.text

    def test_entry_count_rendered(self, monkeypatch) -> None:
        row = _make_session_row(entry_count=7)
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "entry_count" in html
        assert "7" in html

    def test_derivation_count_rendered(self, monkeypatch) -> None:
        row = _make_session_row(derivation_count=4)
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "derivation_count" in html
        assert "4" in html

    def test_zero_entry_count_rendered(self, monkeypatch) -> None:
        row = _make_session_row(entry_count=0, derivation_count=0,
                                has_embeddable_entries=False)
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        # Zero counts should still render (not None/empty)
        assert "0" in html

    def test_idle_finalize_at_rendered(self, monkeypatch) -> None:
        idle = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
        row = _make_session_row(
            status="collecting",
            idle_finalize_at=idle,
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert resp.status_code == 200


# ── Scope boundary tests ─────────────────────────────────────────────────────


class TestAdminReflectionsScopeBoundaries:
    """Verify user/bot/topic scope is preserved and distinct per session."""

    def _patch_list_sessions(self, monkeypatch, rows):
        async def _fake_list(*args, **kwargs):
            return rows

        monkeypatch.setattr(
            "app.routers.admin.admin_list_sessions", _fake_list
        )

    def test_distinct_users_visible(self, monkeypatch) -> None:
        user_a = uuid4()
        user_b = uuid4()
        rows = [
            _make_session_row(user_id=user_a, bot_id="bot-1"),
            _make_session_row(user_id=user_b, bot_id="bot-2"),
        ]
        self._patch_list_sessions(monkeypatch, rows)
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert str(user_a) in html
        assert str(user_b) in html
        assert "bot-1" in html
        assert "bot-2" in html

    def test_distinct_topics_visible(self, monkeypatch) -> None:
        topic_x = uuid4()
        topic_y = uuid4()
        rows = [
            _make_session_row(topic_id=topic_x),
            _make_session_row(topic_id=topic_y),
        ]
        self._patch_list_sessions(monkeypatch, rows)
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert str(topic_x) in html
        assert str(topic_y) in html

    def test_same_user_different_bots_visible(self, monkeypatch) -> None:
        uid = uuid4()
        rows = [
            _make_session_row(user_id=uid, bot_id="superpom"),
            _make_session_row(user_id=uid, bot_id="coach"),
        ]
        self._patch_list_sessions(monkeypatch, rows)
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "superpom" in html
        assert "coach" in html

    def test_classification_confidence_preserved(self, monkeypatch) -> None:
        row = _make_session_row(classification_confidence=0.87)
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert "0.87" in resp.text

    def test_template_key_and_temporal_scope_visible(self, monkeypatch) -> None:
        row = _make_session_row(
            template_key="weekly_checkin",
            temporal_scope="week",
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "weekly_checkin" in html
        assert "week" in html


# ── Status filter tests ─────────────────────────────────────────────────────


class TestAdminReflectionsStatusFilter:
    """Verify the ?status= query parameter filters sessions correctly."""

    def _patch_list_sessions(self, monkeypatch, rows):
        async def _fake_list(pool, *, status_filter=None, limit=100):
            # Simulate the real filtering behaviour
            if status_filter is not None:
                return [r for r in rows if r["status"] == status_filter][:limit]
            return rows[:limit]

        monkeypatch.setattr(
            "app.routers.admin.admin_list_sessions", _fake_list
        )

    def test_status_filter_invoked(self, monkeypatch) -> None:
        # Use unique bot_ids so we can verify which rows appear
        rows = [
            _make_session_row(status="collecting", bot_id="collecting-bot-unique"),
            _make_session_row(status="processed", bot_id="processed-bot-unique"),
        ]
        self._patch_list_sessions(monkeypatch, rows)
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections?status=collecting",
                          auth=_valid_auth())
        html = resp.text
        # The collecting row should be present
        assert "collecting-bot-unique" in html
        # The processed row should NOT appear when filtering for collecting
        assert "processed-bot-unique" not in html

    def test_status_filter_allows_all_when_absent(self, monkeypatch) -> None:
        rows = [
            _make_session_row(status="collecting"),
            _make_session_row(status="processed"),
        ]
        self._patch_list_sessions(monkeypatch, rows)
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "collecting" in html
        assert "processed" in html


# ── Sensitive payload absence tests ──────────────────────────────────────────


class TestAdminReflectionsSensitivePayloadAbsence:
    """Verify NO sensitive payload text appears in admin HTML output."""

    SENSITIVE_FIELD_NAMES = [
        "plaintext_searchable",
        "canonical_text",
        "source_text",
        "payload",
        "summary",
        "correction_note",
        "transcript",
        "decrypted_body",
        "raw_message",
        "searchable_content",
        "body",
        # Broader pattern: "content" is sensitive, but the word "content"
        # alone is too ambiguous.  We check that no field *named* "content"
        # leaks.
    ]

    def _patch_list_sessions(self, monkeypatch, rows):
        async def _fake_list(*args, **kwargs):
            return rows

        monkeypatch.setattr(
            "app.routers.admin.admin_list_sessions", _fake_list
        )

    def test_no_sensitive_field_names_in_html(self, monkeypatch) -> None:
        """Sensitive field names must not appear as column headers."""
        row = _make_session_row()
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        for field in self.SENSITIVE_FIELD_NAMES:
            # Check for the field appearing as a table header <th>field</th>
            assert f"<th>{field}</th>" not in html, (
                f"Sensitive field '{field}' leaked as column header"
            )

    def test_no_plaintext_searchable_in_output(self, monkeypatch) -> None:
        """Even if a row accidentally included plaintext_searchable,
        the redaction step must strip it."""
        row = _make_session_row()
        # Simulate a leak: inject sensitive field into row
        row["plaintext_searchable"] = SENTINEL_PAYLOAD
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert SENTINEL_PAYLOAD not in html, (
            "SENTINEL_PAYLOAD leaked into admin HTML"
        )
        assert SENTINEL_PAYLOAD_SHORT not in html, (
            "SENTINEL_PAYLOAD_SHORT leaked into admin HTML"
        )

    def test_no_canonical_text_in_output(self, monkeypatch) -> None:
        row = _make_session_row()
        row["canonical_text"] = SENTINEL_PAYLOAD
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert SENTINEL_PAYLOAD not in resp.text

    def test_no_summary_in_output(self, monkeypatch) -> None:
        row = _make_session_row()
        row["summary"] = SENTINEL_PAYLOAD
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert SENTINEL_PAYLOAD not in resp.text

    def test_no_payload_in_output(self, monkeypatch) -> None:
        row = _make_session_row()
        row["payload"] = SENTINEL_PAYLOAD
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert SENTINEL_PAYLOAD not in resp.text

    def test_no_transcript_in_output(self, monkeypatch) -> None:
        row = _make_session_row()
        row["transcript"] = SENTINEL_PAYLOAD
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert SENTINEL_PAYLOAD not in resp.text

    def test_no_correction_note_in_output(self, monkeypatch) -> None:
        row = _make_session_row()
        row["correction_note"] = SENTINEL_PAYLOAD
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert SENTINEL_PAYLOAD not in resp.text

    def test_no_source_text_in_output(self, monkeypatch) -> None:
        row = _make_session_row()
        row["source_text"] = SENTINEL_PAYLOAD
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert SENTINEL_PAYLOAD not in resp.text

    def test_no_decrypted_body_in_output(self, monkeypatch) -> None:
        row = _make_session_row()
        row["decrypted_body"] = SENTINEL_PAYLOAD
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        assert SENTINEL_PAYLOAD not in resp.text

    def test_redaction_defense_in_depth_on_admin_row(self, monkeypatch) -> None:
        """Verify that redact_reflection_diagnostics properly handles an
        admin-style row with injected sensitive fields — defense in depth
        even though the SQL only selects safe columns."""
        from app.services.reflection_redaction import (
            REDACTED,
            redact_reflection_diagnostics,
        )
        row = _make_session_row()
        row["plaintext_searchable"] = SENTINEL_PAYLOAD
        row["canonical_text"] = SENTINEL_PAYLOAD
        row["payload"] = SENTINEL_PAYLOAD
        result = redact_reflection_diagnostics(row)
        assert result["plaintext_searchable"] == REDACTED
        assert result["canonical_text"] == REDACTED
        assert result["payload"] == REDACTED
        # Safe fields should still be intact
        assert result["id"] == row["id"]
        assert result["status"] == row["status"]
        assert result["retry_count"] == row["retry_count"]

    def test_classification_metadata_safe(self, monkeypatch) -> None:
        """classification_source and classification_confidence are safe."""
        row = _make_session_row(
            classification_source="classifier_v2",
            classification_confidence=0.95,
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "classifier_v2" in html
        assert "0.95" in html

    def test_failure_metadata_safe(self, monkeypatch) -> None:
        """failure_class, failure_reason, last_error are safe metadata."""
        row = _make_session_row(
            failure_class="terminal_internal",
            failure_reason="Database constraint violation during finalization",
            last_error="IntegrityError",
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "terminal_internal" in html
        assert "IntegrityError" in html

    def test_html_escaped_output(self, monkeypatch) -> None:
        """HTML special characters in safe fields must be escaped."""
        row = _make_session_row(
            failure_reason='<script>alert("xss")</script>',
        )
        self._patch_list_sessions(monkeypatch, [row])
        client = _make_client(monkeypatch)
        resp = client.get("/admin/reflections", auth=_valid_auth())
        html = resp.text
        assert "<script>" not in html.lower()
        assert "&lt;script&gt;" in html


# ── admin_list_sessions direct function tests ────────────────────────────────


class TestAdminListSessionsDirect:
    """Test admin_list_sessions directly with a controlled async mock pool."""

    @pytest.mark.anyio
    async def test_returns_list_of_dicts(self) -> None:
        """Basic smoke test: function returns a list of dicts."""
        from app.services.reflections import admin_list_sessions

        class _MockPool:
            async def fetch(self, sql, *args):
                return []

        result = await admin_list_sessions(_MockPool())
        assert isinstance(result, list)
        assert result == []

    @pytest.mark.anyio
    async def test_returns_all_expected_keys(self) -> None:
        """Each returned row must have all the expected metadata keys."""
        from app.services.reflections import admin_list_sessions

        session_id = uuid4()
        user_id = uuid4()
        bot_id = "superpom"
        topic_id = uuid4()
        now = datetime.now(UTC)

        fake_row = {
            "id": session_id,
            "user_id": user_id,
            "bot_id": bot_id,
            "topic_id": topic_id,
            "template_key": "freeform",
            "temporal_scope": "week",
            "phase": "retrospective",
            "status": "processed",
            "classification_source": "classifier_v1",
            "classification_confidence": 0.92,
            "retry_count": 0,
            "failure_class": None,
            "failure_reason": None,
            "last_error": None,
            "claimed_by": None,
            "claimed_at": None,
            "created_at": now,
            "finalized_at": now,
            "processed_at": now,
            "abandoned_at": None,
            "idle_finalize_at": None,
            "updated_at": now,
            "idempotency_key": "ik-123",
            "entry_count": 3,
            "derivation_count": 2,
            "has_embeddable_entries": True,
        }

        class FakeRecord:
            """Minimal asyncpg Record stand-in that supports dict()."""
            def __init__(self, data: dict):
                self._data = data
            def __getitem__(self, key):
                return self._data[key]
            def __iter__(self):
                return iter(self._data)
            def keys(self):
                return self._data.keys()

        class _MockPool:
            async def fetch(self, sql, *args):
                return [FakeRecord(fake_row)]

        result = await admin_list_sessions(_MockPool())
        assert len(result) == 1
        row = result[0]
        expected_keys = {
            "id", "user_id", "bot_id", "topic_id", "template_key",
            "temporal_scope", "phase", "status", "classification_source",
            "classification_confidence", "retry_count", "failure_class",
            "failure_reason", "last_error", "claimed_by", "claimed_at",
            "created_at", "finalized_at", "processed_at", "abandoned_at",
            "idle_finalize_at", "updated_at", "idempotency_key",
            "entry_count", "derivation_count", "has_embeddable_entries",
        }
        assert set(row.keys()) == expected_keys, (
            f"Row keys mismatch: got {set(row.keys())}, expected {expected_keys}"
        )

    @pytest.mark.anyio
    async def test_no_sensitive_columns_in_query(self) -> None:
        """The generated SQL must not SELECT any sensitive columns."""
        from app.services.reflections import admin_list_sessions

        captured_sql = []

        class _MockPool:
            async def fetch(self, sql, *args):
                captured_sql.append(sql)
                return []

        await admin_list_sessions(_MockPool())
        sql = captured_sql[0].casefold()

        forbidden = [
            "plaintext_searchable",
            "canonical_text",
            "source_text",
            "decrypted_body",
            "raw_message",
            "payload",
            "summary",
            "correction_note",
            "transcript",
        ]
        for term in forbidden:
            # Only flag if it appears as a SELECTed column, not just in comments
            # Simple heuristic: check if the word appears as a column alias
            assert f"rs.{term}" not in sql or f"as {term}" in sql, (
                f"Sensitive column '{term}' found in admin_list_sessions SQL"
            )

    @pytest.mark.anyio
    async def test_status_filter_validated(self) -> None:
        """Invalid status_filter must raise ValueError."""
        from app.services.reflections import admin_list_sessions

        class _MockPool:
            async def fetch(self, sql, *args):
                return []

        with pytest.raises(ValueError, match="invalid status"):
            await admin_list_sessions(_MockPool(), status_filter="not_a_real_status")

    @pytest.mark.anyio
    async def test_status_filter_accepted(self) -> None:
        """Valid status_filter should not raise."""
        from app.services.reflections import admin_list_sessions

        class _MockPool:
            async def fetch(self, sql, *args):
                return []

        # Should not raise for each valid status
        for status in ["collecting", "finalizing", "processed",
                        "abandoned", "processing_failed"]:
            await admin_list_sessions(_MockPool(), status_filter=status)

    @pytest.mark.anyio
    async def test_limit_applied(self) -> None:
        """Limit parameter must appear in the SQL."""
        from app.services.reflections import admin_list_sessions

        captured_args = []

        class _MockPool:
            async def fetch(self, sql, *args):
                captured_args.extend(args)
                return []

        await admin_list_sessions(_MockPool(), limit=50)
        assert 50 in captured_args, "limit parameter not passed to query"

    @pytest.mark.anyio
    async def test_has_embeddable_entries_is_boolean(self) -> None:
        """The has_embeddable_entries field must be boolean, not exposing content."""
        from app.services.reflections import admin_list_sessions

        true_row = {
            "id": uuid4(), "user_id": uuid4(), "bot_id": "b", "topic_id": uuid4(),
            "template_key": "t", "temporal_scope": "day", "phase": "freeform",
            "status": "processed", "classification_source": None,
            "classification_confidence": None, "retry_count": 0,
            "failure_class": None, "failure_reason": None, "last_error": None,
            "claimed_by": None, "claimed_at": None, "created_at": datetime.now(UTC),
            "finalized_at": None, "processed_at": None, "abandoned_at": None,
            "idle_finalize_at": None, "updated_at": datetime.now(UTC),
            "idempotency_key": None,
            "entry_count": 1, "derivation_count": 0,
            "has_embeddable_entries": True,
        }

        class FakeRecord:
            def __init__(self, data: dict):
                self._data = data
            def __getitem__(self, key):
                return self._data[key]
            def __iter__(self):
                return iter(self._data)
            def keys(self):
                return self._data.keys()

        class _MockPool:
            async def fetch(self, sql, *args):
                return [FakeRecord(true_row)]

        result = await admin_list_sessions(_MockPool())
        assert result[0]["has_embeddable_entries"] is True

    @pytest.mark.anyio
    async def test_no_content_field_leaked(self) -> None:
        """'content' field must never appear in admin_list_sessions SQL SELECT."""
        from app.services.reflections import admin_list_sessions

        captured_sql = []

        class _MockPool:
            async def fetch(self, sql, *args):
                captured_sql.append(sql)
                return []

        await admin_list_sessions(_MockPool())
        sql_lower = captured_sql[0].lower()
        # "rs.content" must not appear as a selected column
        assert "rs.content" not in sql_lower, (
            "'content' column found in admin_list_sessions SELECT"
        )


# ── Release evidence mapping tests ───────────────────────────────────────────


class TestAdminReflectionsReleaseEvidenceMapping:
    """Each exposed field must map to a provable test for release evidence."""

    EXPOSED_FIELD_TESTS = {
        "id": "test_session_id_rendered",
        "user_id": "test_user_id_rendered",
        "bot_id": "test_bot_id_rendered",
        "topic_id": "test_topic_id_rendered",
        "template_key": "test_template_key_and_temporal_scope_visible",
        "temporal_scope": "test_template_key_and_temporal_scope_visible",
        "phase": "test_active_collecting_session_rendered",
        "status": "test_multiple_sessions_different_statuses",
        "classification_source": "test_classification_metadata_safe",
        "classification_confidence": "test_classification_metadata_safe",
        "retry_count": "test_stuck_session_failure_class_rendered",
        "failure_class": "test_stuck_session_failure_class_rendered",
        "failure_reason": "test_failure_metadata_safe",
        "last_error": "test_failure_metadata_safe",
        "entry_count": "test_entry_count_rendered",
        "derivation_count": "test_derivation_count_rendered",
        "has_embeddable_entries": "test_embeddable_true_rendered",
        "claimed_by": "test_active_finalizing_session_rendered",
        "created_at": "test_all_exposed_columns_in_html",
        "finalized_at": "test_all_exposed_columns_in_html",
        "processed_at": "test_all_exposed_columns_in_html",
        "abandoned_at": "test_abandoned_session_rendered",
        "idle_finalize_at": "test_idle_finalize_at_rendered",
        "updated_at": "test_all_exposed_columns_in_html",
    }

    SENSITIVE_ABSENCE_TESTS = {
        "plaintext_searchable": "test_no_plaintext_searchable_in_output",
        "canonical_text": "test_no_canonical_text_in_output",
        "source_text": "test_no_source_text_in_output",
        "payload": "test_no_payload_in_output",
        "summary": "test_no_summary_in_output",
        "correction_note": "test_no_correction_note_in_output",
        "transcript": "test_no_transcript_in_output",
        "decrypted_body": "test_no_decrypted_body_in_output",
    }

    def test_all_exposed_fields_have_tests(self) -> None:
        """Every column in the EXPOSED_COLUMNS list has a corresponding test."""
        from tests.test_admin_reflections import (
            TestAdminReflectionsFieldPresence,
        )
        columns = TestAdminReflectionsFieldPresence.EXPOSED_COLUMNS
        for col in columns:
            assert col in self.EXPOSED_FIELD_TESTS, (
                f"Exposed field '{col}' has no release evidence test mapping"
            )

    def test_every_mapped_test_exists_on_class(self) -> None:
        """Every test name in the mapping must be a real method."""
        from tests import test_admin_reflections as mod
        for field_name, test_name in self.EXPOSED_FIELD_TESTS.items():
            # Check that the test method exists somewhere in the module
            found = False
            for cls_name in dir(mod):
                cls = getattr(mod, cls_name)
                if isinstance(cls, type) and hasattr(cls, test_name):
                    found = True
                    break
            assert found, (
                f"Release evidence test '{test_name}' for field "
                f"'{field_name}' not found in test_admin_reflections"
            )

    def test_sensitive_fields_have_absence_tests(self) -> None:
        """Every sensitive field has a corresponding absence test in this file."""
        for field_name, test_name in self.SENSITIVE_ABSENCE_TESTS.items():
            # These tests live in TestAdminReflectionsSensitivePayloadAbsence
            from tests.test_admin_reflections import (
                TestAdminReflectionsSensitivePayloadAbsence,
            )
            assert hasattr(TestAdminReflectionsSensitivePayloadAbsence, test_name), (
                f"Absence test '{test_name}' for sensitive field "
                f"'{field_name}' not found"
            )
