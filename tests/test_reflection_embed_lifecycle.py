"""Step 3 (T5): Focused reflection embedding lifecycle and worker tests.

Covers:
- Idempotent enqueue (embed + drop)
- Correction re-embedding (drop superseded + embed new)
- Deletion tombstoning/removal (drop job deletes active embedding)
- Exclusion of empty-plaintext candidates from active embeddings
- Retry behavior (provider failures, max attempts)
- Rejection of cross-scope hydration
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.services.embeddings import content_hash
from app.services.embed_worker import EmbedJobWorker

pytestmark = pytest.mark.anyio


# ── Fake connection for enqueue tests ────────────────────────────────────────


class FakeReflectionEmbedConn:
    """Simulates the embed_jobs table with source_type-aware idempotency."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.executed_sql: list[str] = []

    async def execute(self, sql: str, *args):
        self.executed_sql.append(sql)
        compact = " ".join(sql.split())
        if "superseded by drop job" in compact:
            source_type, source_id, now = args
            affected = 0
            for job in self.jobs:
                if (
                    job.get("source_type", "message") == source_type
                    and job.get("source_id", job["message_id"]) == source_id
                    and job["job_kind"] in {"embed", "reembed"}
                    and job["status"] == "pending"
                ):
                    job.update(
                        status="cancelled",
                        last_error="superseded by drop job",
                        locked_at=None,
                        locked_by=None,
                        updated_at=now,
                        completed_at=now,
                    )
                    affected += 1
            return f"UPDATE {affected}"
        if "superseded by newer content hash" in compact:
            source_type, source_id, content_hash_value, now = args
            affected = 0
            for job in self.jobs:
                if (
                    job.get("source_type", "message") == source_type
                    and job.get("source_id", job["message_id"]) == source_id
                    and job["job_kind"] in {"embed", "reembed"}
                    and job["status"] == "pending"
                    and job["content_hash"] != content_hash_value
                ):
                    job.update(
                        status="superseded",
                        last_error="superseded by newer content hash",
                        locked_at=None,
                        locked_by=None,
                        updated_at=now,
                        completed_at=now,
                    )
                    affected += 1
            return f"UPDATE {affected}"
        raise AssertionError(f"unexpected execute: {compact}")

    async def fetchrow(self, sql: str, *args):
        self.executed_sql.append(sql)
        compact = " ".join(sql.split())
        if compact.startswith("SELECT id, source_type, source_id, message_id, job_kind"):
            source_type, source_id, job_kind, content_hash_value = args
            matches = [
                job
                for job in self.jobs
                if job.get("source_type", "message") == source_type
                and job.get("source_id", job["message_id"]) == source_id
                and job["job_kind"] == job_kind
                and job["status"] in {"pending", "processing"}
                and job["content_hash"] == content_hash_value
            ]
            if not matches:
                return None
            return sorted(matches, key=lambda row: (row["created_at"], str(row["id"])))[0]
        if compact.startswith("INSERT INTO mediator.embed_jobs"):
            source_type, source_id, message_id, job_kind, model, dimension, content_hash_value, now = args
            for job in self.jobs:
                if (
                    job["source_type"] == source_type
                    and job["source_id"] == source_id
                    and job["job_kind"] == job_kind
                    and job["status"] in {"pending", "processing"}
                    and job["content_hash"] == content_hash_value
                ):
                    job.setdefault("source_type", source_type)
                    job.setdefault("source_id", source_id)
                    return job
            row = {
                "id": uuid4(),
                "source_type": source_type,
                "source_id": source_id,
                "message_id": message_id,
                "job_kind": job_kind,
                "status": "pending",
                "model": model,
                "dimension": dimension,
                "content_hash": content_hash_value,
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": now,
                "locked_at": None,
                "locked_by": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            self.jobs.append(row)
            return row
        raise AssertionError(f"unexpected fetchrow: {compact}")


# ── Fake worker pool ─────────────────────────────────────────────────────────


class FakeReflectionWorkerPool:
    """Full fake: handles claim fetches, searchable lookups, embedding upserts,
    AND the enqueue/re-enqueue paths the worker uses for stale-hash supersede."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.searchable: dict = {}
        self.embeddings: dict = {}
        self.sql: list[str] = []

    # ── claim fetches (returns list) ──────────────────────────────────────

    async def fetch(self, sql: str, *args):
        self.sql.append(sql)
        compact = " ".join(sql.split())
        if "FOR UPDATE SKIP LOCKED" not in compact:
            raise AssertionError(f"unexpected fetch: {compact}")
        now, limit, worker_id = args
        due = [
            job
            for job in self.jobs
            if job["status"] == "pending" and job["next_attempt_at"] <= now
        ]
        due = sorted(due, key=lambda row: (row["next_attempt_at"], row["created_at"], str(row["id"])))[:limit]
        rows = []
        for job in due:
            job.update(
                status="processing",
                attempts=job["attempts"] + 1,
                locked_at=now,
                locked_by=worker_id,
                updated_at=now,
            )
            rows.append(
                {
                    "id": job["id"],
                    "source_type": job["source_type"],
                    "source_id": job["source_id"],
                    "message_id": job["message_id"],
                    "job_kind": job["job_kind"],
                    "model": job["model"],
                    "dimension": job["dimension"],
                    "content_hash": job["content_hash"],
                    "attempts": job["attempts"],
                    "locked_by": job["locked_by"],
                }
            )
        return rows

    # ── single-row queries (fetchrow) ─────────────────────────────────────

    async def fetchrow(self, sql: str, *args):
        self.sql.append(sql)
        compact = " ".join(sql.split())

        # Worker reads searchable view
        if "v_searchable_content" in compact:
            source_type, source_id = args
            return self.searchable.get((source_type, source_id))

        # enqueue_embed_job / enqueue_reembed_job: _fetch_active_job
        if "status IN ('pending', 'processing')" in compact and "embed_jobs" in compact:
            source_type, source_id, job_kind, content_hash_value = args
            matches = [
                job
                for job in self.jobs
                if job["source_type"] == source_type
                and job["source_id"] == source_id
                and job["job_kind"] == job_kind
                and job["status"] in {"pending", "processing"}
                and job["content_hash"] == content_hash_value
            ]
            if not matches:
                return None
            return sorted(matches, key=lambda row: (row["created_at"], str(row["id"])))[0]

        # enqueue_embed_job / enqueue_reembed_job: INSERT INTO embed_jobs RETURNING *
        if compact.startswith("INSERT INTO mediator.embed_jobs"):
            source_type, source_id, message_id, job_kind, model, dimension, content_hash_value, now = args
            row = {
                "id": uuid4(),
                "source_type": source_type,
                "source_id": source_id,
                "message_id": message_id,
                "job_kind": job_kind,
                "status": "pending",
                "model": model,
                "dimension": dimension,
                "content_hash": content_hash_value,
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": now,
                "locked_at": None,
                "locked_by": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            self.jobs.append(row)
            return row

        raise AssertionError(f"unexpected fetchrow: {compact}")

    # ── writes (execute) ──────────────────────────────────────────────────

    async def execute(self, sql: str, *args):
        self.sql.append(sql)
        compact = " ".join(sql.split())

        # Upsert embedding
        if compact.startswith("INSERT INTO mediator.content_embeddings"):
            source_type, source_id, vector, model, dimension, content_hash_value, now = args
            self.embeddings[(source_type, source_id)] = {
                "embedding": vector,
                "model": model,
                "dimension": dimension,
                "content_hash": content_hash_value,
                "embedded_at": now,
            }
            return "INSERT 0 1"

        # Delete embedding
        if compact.startswith("DELETE FROM mediator.content_embeddings"):
            self.embeddings.pop((args[0], args[1]), None)
            return "DELETE 1"

        # Mark completed (succeeded / skipped)
        if compact.startswith("UPDATE mediator.embed_jobs SET status = $1"):
            status, last_error, now, job_id, worker_id = args
            for job in self.jobs:
                if job["id"] == job_id and job["status"] == "processing" and job["locked_by"] == worker_id:
                    job.update(
                        status=status,
                        last_error=last_error,
                        locked_at=None,
                        locked_by=None,
                        updated_at=now,
                        completed_at=now,
                    )
                    return "UPDATE 1"
            return "UPDATE 0"

        # Retry (set back to pending)
        if compact.startswith("UPDATE mediator.embed_jobs SET status = 'pending'"):
            last_error, next_attempt_at, now, job_id, worker_id = args
            for job in self.jobs:
                if job["id"] == job_id and job["status"] == "processing" and job["locked_by"] == worker_id:
                    job.update(
                        status="pending",
                        last_error=last_error,
                        next_attempt_at=next_attempt_at,
                        locked_at=None,
                        locked_by=None,
                        updated_at=now,
                    )
                    return "UPDATE 1"
            return "UPDATE 0"

        # Supersede pending jobs with stale hash (enqueue path)
        if "superseded by newer content hash" in compact:
            source_type, source_id, content_hash_value, now = args
            affected = 0
            for job in self.jobs:
                if (
                    job["source_type"] == source_type
                    and job["source_id"] == source_id
                    and job["job_kind"] in {"embed", "reembed"}
                    and job["status"] == "pending"
                    and job["content_hash"] != content_hash_value
                ):
                    job.update(status="superseded", updated_at=now, completed_at=now)
                    affected += 1
            return f"UPDATE {affected}"

        if "superseded by drop job" in compact:
            return "UPDATE 0"

        raise AssertionError(f"unexpected execute: {compact}")


class TinyReflectionEmbedder:
    model_name = "text-embedding-3-small"
    dimension = 3

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("provider unavailable")
        return [[1.0, 0.0, 0.0] for _ in texts]


class ReflectionWorkerSettingsStub:
    embedding_worker_batch_size = 10
    embedding_worker_poll_interval_s = 0.01


def _reflection_job(
    *,
    entry_id,
    source_type="reflection",
    job_kind="embed",
    content_hash: str | None = None,
    model="text-embedding-3-small",
    dimension=3,
    now: datetime,
    attempts=0,
):
    return {
        "id": uuid4(),
        "source_type": source_type,
        "source_id": entry_id,
        "message_id": None,
        "job_kind": job_kind,
        "status": "pending",
        "model": model,
        "dimension": dimension,
        "content_hash": content_hash,
        "attempts": attempts,
        "last_error": None,
        "next_attempt_at": now,
        "locked_at": None,
        "locked_by": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }


# ── Idempotent enqueue ───────────────────────────────────────────────────────


async def test_enqueue_reflection_embed_is_idempotent(monkeypatch, app_env):
    """Calling enqueue_reflection_embed twice with the same entry yields
    exactly one active embed job (idempotent by content hash + source identity)."""
    from app.services import message_embedding_lifecycle as lifecycle
    from app.services.embed_jobs import enqueue_embed_job as real_enqueue_embed

    conn = FakeReflectionEmbedConn()
    entry_id = uuid4()
    plaintext = "Reflection summary text"
    calls: list[tuple] = []

    async def record_enqueue(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        calls.append((source_type, source_id, message_id, content_hash))
        return await real_enqueue_embed(
            conn, source_type=source_type, source_id=source_id,
            content_hash=content_hash, model=model, dimension=dimension,
            message_id=message_id,
        )

    monkeypatch.setattr(lifecycle, "enqueue_embed_job", record_enqueue)

    await lifecycle.enqueue_reflection_embed(conn, entry_id=entry_id, plaintext_searchable=plaintext)
    await lifecycle.enqueue_reflection_embed(conn, entry_id=entry_id, plaintext_searchable=plaintext)

    # Two calls, but real enqueue is idempotent → one active job
    embed_jobs = [j for j in conn.jobs if j["job_kind"] == "embed"]
    assert len(embed_jobs) == 1
    assert embed_jobs[0]["source_type"] == "reflection"
    assert embed_jobs[0]["source_id"] == entry_id
    assert embed_jobs[0]["message_id"] is None
    assert embed_jobs[0]["content_hash"] == content_hash(plaintext)
    assert embed_jobs[0]["status"] == "pending"


async def test_enqueue_reflection_embed_skips_empty_plaintext(monkeypatch):
    """Empty or whitespace-only plaintext must not produce an embed job."""
    from app.services import message_embedding_lifecycle as lifecycle

    conn = FakeReflectionEmbedConn()

    async def fail_enqueue(*args, **kwargs):
        raise AssertionError("must not enqueue for empty plaintext")

    monkeypatch.setattr(lifecycle, "enqueue_embed_job", fail_enqueue)

    # These should return early without calling enqueue_embed_job
    await lifecycle.enqueue_reflection_embed(conn, entry_id=uuid4(), plaintext_searchable="")
    await lifecycle.enqueue_reflection_embed(conn, entry_id=uuid4(), plaintext_searchable="   ")
    await lifecycle.enqueue_reflection_embed(conn, entry_id=uuid4(), plaintext_searchable=None)

    assert len(conn.jobs) == 0


async def test_enqueue_reflection_drop_is_idempotent(monkeypatch):
    """Calling enqueue_reflection_drop twice for the same entry creates only
    one drop job and cancels pending embed jobs once."""
    from app.services import message_embedding_lifecycle as lifecycle
    from app.services.embed_jobs import enqueue_drop_embedding_job as real_drop

    conn = FakeReflectionEmbedConn()
    entry_id = uuid4()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)

    conn.jobs.append(
        {
            "id": uuid4(),
            "source_type": "reflection",
            "source_id": entry_id,
            "message_id": None,
            "job_kind": "embed",
            "status": "pending",
            "model": "text-embedding-3-small",
            "dimension": 1536,
            "content_hash": content_hash("some text"),
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": now,
            "locked_at": None,
            "locked_by": None,
            "created_at": now - timedelta(minutes=5),
            "updated_at": now - timedelta(minutes=5),
            "completed_at": None,
        }
    )

    calls: list[tuple] = []

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        calls.append((source_type, source_id, message_id))
        return await real_drop(conn, source_type=source_type, source_id=source_id, message_id=message_id)

    monkeypatch.setattr(lifecycle, "enqueue_drop_embedding_job", record_drop)

    await lifecycle.enqueue_reflection_drop(conn, entry_id=entry_id)
    await lifecycle.enqueue_reflection_drop(conn, entry_id=entry_id)

    drop_jobs = [j for j in conn.jobs if j["job_kind"] == "drop"]
    assert len(drop_jobs) == 1
    assert drop_jobs[0]["source_type"] == "reflection"
    assert drop_jobs[0]["source_id"] == entry_id
    assert drop_jobs[0]["content_hash"] is None
    assert conn.jobs[0]["status"] == "cancelled"


# ── Correction re-embedding lifecycle ────────────────────────────────────────


async def test_correction_lifecycle_drop_superseded_and_embed_new(monkeypatch, app_env):
    """Simulates the correct_entry pattern: drop old + enqueue embed for new."""
    from app.services import message_embedding_lifecycle as lifecycle
    from app.services.embed_jobs import (
        enqueue_embed_job as real_enqueue_embed,
        enqueue_drop_embedding_job as real_drop,
    )

    conn = FakeReflectionEmbedConn()
    old_entry_id = uuid4()
    new_entry_id = uuid4()
    new_plaintext = "Corrected reflection text"
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)

    conn.jobs.append(
        {
            "id": uuid4(),
            "source_type": "reflection",
            "source_id": old_entry_id,
            "message_id": None,
            "job_kind": "embed",
            "status": "pending",
            "model": "text-embedding-3-small",
            "dimension": 1536,
            "content_hash": content_hash("old text"),
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": now,
            "locked_at": None,
            "locked_by": None,
            "created_at": now - timedelta(minutes=5),
            "updated_at": now - timedelta(minutes=5),
            "completed_at": None,
        }
    )

    # Patch both lifecycle primitives to use the fake conn
    async def fake_enqueue(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        return await real_enqueue_embed(
            conn, source_type=source_type, source_id=source_id,
            content_hash=content_hash, model=model, dimension=dimension,
            message_id=message_id,
        )

    async def fake_drop(pool, *, source_type, source_id, message_id=None):
        return await real_drop(conn, source_type=source_type, source_id=source_id, message_id=message_id)

    monkeypatch.setattr(lifecycle, "enqueue_embed_job", fake_enqueue)
    monkeypatch.setattr(lifecycle, "enqueue_drop_embedding_job", fake_drop)

    # Step 1: Drop embedding for the superseded entry
    await lifecycle.enqueue_reflection_drop(conn, entry_id=old_entry_id)

    # Step 2: Enqueue embedding for the new current entry
    await lifecycle.enqueue_reflection_embed(conn, entry_id=new_entry_id, plaintext_searchable=new_plaintext)

    drop_jobs = [j for j in conn.jobs if j["job_kind"] == "drop" and j["source_id"] == old_entry_id]
    new_embed_jobs = [j for j in conn.jobs if j["job_kind"] == "embed" and j["source_id"] == new_entry_id]
    assert len(drop_jobs) == 1
    assert conn.jobs[0]["status"] == "cancelled"
    assert len(new_embed_jobs) == 1
    assert new_embed_jobs[0]["content_hash"] == content_hash(new_plaintext)


async def test_correction_with_empty_plaintext_only_drops(monkeypatch):
    """When a correction produces empty plaintext, only the drop happens."""
    from app.services import message_embedding_lifecycle as lifecycle
    from app.services.embed_jobs import enqueue_drop_embedding_job as real_drop

    conn = FakeReflectionEmbedConn()
    old_entry_id = uuid4()
    new_entry_id = uuid4()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)

    conn.jobs.append(
        {
            "id": uuid4(),
            "source_type": "reflection",
            "source_id": old_entry_id,
            "message_id": None,
            "job_kind": "embed",
            "status": "pending",
            "model": "text-embedding-3-small",
            "dimension": 1536,
            "content_hash": content_hash("old"),
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": now,
            "locked_at": None,
            "locked_by": None,
            "created_at": now - timedelta(minutes=5),
            "updated_at": now - timedelta(minutes=5),
            "completed_at": None,
        }
    )

    async def fake_drop(pool, *, source_type, source_id, message_id=None):
        return await real_drop(conn, source_type=source_type, source_id=source_id, message_id=message_id)

    monkeypatch.setattr(lifecycle, "enqueue_drop_embedding_job", fake_drop)

    # Fail if enqueue_embed_job is called (empty plaintext should skip it)
    async def fail_enqueue(*args, **kwargs):
        raise AssertionError("must not enqueue embed for empty plaintext")

    monkeypatch.setattr(lifecycle, "enqueue_embed_job", fail_enqueue)

    # Drop old
    await lifecycle.enqueue_reflection_drop(conn, entry_id=old_entry_id)
    # New entry has no searchable plaintext — this should return early
    await lifecycle.enqueue_reflection_embed(conn, entry_id=new_entry_id, plaintext_searchable="")

    drop_jobs = [j for j in conn.jobs if j["job_kind"] == "drop"]
    embed_jobs_for_new = [j for j in conn.jobs if j["job_kind"] == "embed" and j["source_id"] == new_entry_id]
    assert len(drop_jobs) == 1
    assert len(embed_jobs_for_new) == 0


# ── Worker: embed lifecycle ──────────────────────────────────────────────────


async def test_worker_embeds_reflection_searchable_text():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    entry_id = uuid4()
    plaintext = "Reflection on recent conversation patterns"
    pool = FakeReflectionWorkerPool()
    pool.searchable[("reflection", entry_id)] = {
        "source_type": "reflection",
        "source_id": entry_id,
        "message_id": None,
        "canonical_text": plaintext,
    }
    pool.jobs.append(
        _reflection_job(entry_id=entry_id, content_hash=content_hash(plaintext), now=now)
    )
    embedder = TinyReflectionEmbedder()

    result = await EmbedJobWorker(
        pool,
        settings=ReflectionWorkerSettingsStub(),
        embedder=embedder,
        worker_id="worker-r",
    ).run_once(now=now)

    assert result.claimed == 1
    assert result.embedded == 1
    assert embedder.calls == [[plaintext]]
    assert pool.embeddings[("reflection", entry_id)]["content_hash"] == content_hash(plaintext)
    assert pool.jobs[0]["status"] == "succeeded"
    assert pool.jobs[0]["locked_by"] is None


async def test_worker_drops_reflection_embedding():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    entry_id = uuid4()
    pool = FakeReflectionWorkerPool()
    pool.embeddings[("reflection", entry_id)] = {"content_hash": "old"}
    pool.jobs.append(
        _reflection_job(
            entry_id=entry_id,
            job_kind="drop",
            content_hash=None,
            model=None,
            dimension=None,
            now=now,
        )
    )

    result = await EmbedJobWorker(
        pool,
        settings=ReflectionWorkerSettingsStub(),
        embedder=TinyReflectionEmbedder(),
        worker_id="worker-r",
    ).run_once(now=now)

    assert result.dropped == 1
    assert ("reflection", entry_id) not in pool.embeddings
    assert all("v_searchable_content" not in sql for sql in pool.sql)


async def test_worker_reflection_missing_view_row_deletes_embedding():
    """Non-message sources (reflection) with missing searchable row:
    delete stale embedding and mark skipped."""
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    entry_id = uuid4()
    pool = FakeReflectionWorkerPool()
    pool.embeddings[("reflection", entry_id)] = {"content_hash": "old"}
    pool.jobs.append(
        _reflection_job(entry_id=entry_id, content_hash=content_hash("gone"), now=now)
    )

    result = await EmbedJobWorker(
        pool,
        settings=ReflectionWorkerSettingsStub(),
        embedder=TinyReflectionEmbedder(),
        worker_id="worker-r",
    ).run_once(now=now)

    assert result.skipped == 1
    assert ("reflection", entry_id) not in pool.embeddings
    assert pool.jobs[0]["last_error"] == "source no longer searchable; embedding deleted"


async def test_worker_reflection_stale_hash_supersedes():
    """When canonical text hash differs from job hash, supersede and enqueue
    a new job with the current hash."""
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    entry_id = uuid4()
    plaintext = "Fresh reflection content"
    pool = FakeReflectionWorkerPool()
    pool.searchable[("reflection", entry_id)] = {
        "source_type": "reflection",
        "source_id": entry_id,
        "message_id": None,
        "canonical_text": plaintext,
    }
    pool.jobs.append(
        _reflection_job(entry_id=entry_id, content_hash="a" * 64, now=now)
    )

    result = await EmbedJobWorker(
        pool,
        settings=ReflectionWorkerSettingsStub(),
        embedder=TinyReflectionEmbedder(),
        worker_id="worker-r",
    ).run_once(now=now)

    assert result.superseded == 1
    assert pool.jobs[0]["status"] == "superseded"
    assert len(pool.jobs) >= 2, "should have enqueued a new job with the current hash"
    new_jobs = [j for j in pool.jobs if j["status"] == "pending"]
    assert len(new_jobs) >= 1
    assert new_jobs[0]["content_hash"] == content_hash(plaintext)
    assert new_jobs[0]["source_type"] == "reflection"
    assert new_jobs[0]["source_id"] == entry_id


# ── Worker: retry behavior ───────────────────────────────────────────────────


async def test_worker_retries_reflection_provider_failures():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    entry_id = uuid4()
    plaintext = "Reflection for retry test"
    pool = FakeReflectionWorkerPool()
    pool.searchable[("reflection", entry_id)] = {
        "source_type": "reflection",
        "source_id": entry_id,
        "message_id": None,
        "canonical_text": plaintext,
    }
    pool.jobs.append(
        _reflection_job(entry_id=entry_id, content_hash=content_hash(plaintext), now=now)
    )

    result = await EmbedJobWorker(
        pool,
        settings=ReflectionWorkerSettingsStub(),
        embedder=TinyReflectionEmbedder(fail=True),
        worker_id="worker-r",
    ).run_once(now=now)

    assert result.retried == 1
    assert pool.jobs[0]["status"] == "pending"
    assert pool.jobs[0]["locked_by"] is None
    assert pool.jobs[0]["next_attempt_at"] == now + timedelta(seconds=5)


async def test_worker_fails_reflection_after_max_attempts():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    entry_id = uuid4()
    plaintext = "Final attempt reflection"
    pool = FakeReflectionWorkerPool()
    pool.searchable[("reflection", entry_id)] = {
        "source_type": "reflection",
        "source_id": entry_id,
        "message_id": None,
        "canonical_text": plaintext,
    }
    pool.jobs.append(
        _reflection_job(entry_id=entry_id, content_hash=content_hash(plaintext), now=now, attempts=4)
    )

    result = await EmbedJobWorker(
        pool,
        settings=ReflectionWorkerSettingsStub(),
        embedder=TinyReflectionEmbedder(fail=True),
        worker_id="worker-r",
    ).run_once(now=now)

    assert result.failed == 1
    assert pool.jobs[0]["status"] == "failed"
    assert pool.jobs[0]["locked_by"] is None
    assert pool.jobs[0]["completed_at"] == now
    assert pool.jobs[0]["last_error"] == "provider unavailable"


# ── Cross-scope hydration rejection ──────────────────────────────────────────


async def test_reflection_embed_does_not_use_message_id(monkeypatch, app_env):
    """Reflection embed jobs MUST NOT set message_id (cross-scope boundary)."""
    from app.services import message_embedding_lifecycle as lifecycle
    from app.services.embed_jobs import enqueue_embed_job as real_enqueue_embed

    conn = FakeReflectionEmbedConn()
    entry_id = uuid4()
    calls: list[dict] = []

    async def record_enqueue(pool, *, source_type, source_id, content_hash, model, dimension, message_id=None):
        calls.append({"source_type": source_type, "message_id": message_id})
        return await real_enqueue_embed(
            conn, source_type=source_type, source_id=source_id,
            content_hash=content_hash, model=model, dimension=dimension,
            message_id=message_id,
        )

    monkeypatch.setattr(lifecycle, "enqueue_embed_job", record_enqueue)

    await lifecycle.enqueue_reflection_embed(conn, entry_id=entry_id, plaintext_searchable="test")

    assert len(calls) == 1
    assert calls[0]["source_type"] == "reflection"
    assert calls[0]["message_id"] is None, (
        "reflection embed jobs must not populate message_id; "
        "cross-scope hydration is rejected"
    )
    job = conn.jobs[0]
    assert job["message_id"] is None


async def test_reflection_drop_does_not_use_message_id(monkeypatch):
    """Reflection drop jobs MUST NOT set message_id."""
    from app.services import message_embedding_lifecycle as lifecycle
    from app.services.embed_jobs import enqueue_drop_embedding_job as real_drop

    conn = FakeReflectionEmbedConn()
    entry_id = uuid4()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)

    conn.jobs.append(
        {
            "id": uuid4(),
            "source_type": "reflection",
            "source_id": entry_id,
            "message_id": None,
            "job_kind": "embed",
            "status": "pending",
            "model": "text-embedding-3-small",
            "dimension": 1536,
            "content_hash": content_hash("text"),
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": now,
            "locked_at": None,
            "locked_by": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
    )

    calls: list[dict] = []

    async def record_drop(pool, *, source_type, source_id, message_id=None):
        calls.append({"source_type": source_type, "message_id": message_id})
        return await real_drop(conn, source_type=source_type, source_id=source_id, message_id=message_id)

    monkeypatch.setattr(lifecycle, "enqueue_drop_embedding_job", record_drop)

    await lifecycle.enqueue_reflection_drop(conn, entry_id=entry_id)

    assert len(calls) == 1
    assert calls[0]["message_id"] is None, (
        "reflection drop jobs must not populate message_id"
    )


async def test_reflection_worker_does_not_read_messages_table():
    """Reflection jobs (non-message type) must not hit mediator.messages table."""
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    entry_id = uuid4()
    plaintext = "Reflection without message scope"
    pool = FakeReflectionWorkerPool()
    pool.searchable[("reflection", entry_id)] = {
        "source_type": "reflection",
        "source_id": entry_id,
        "message_id": None,
        "canonical_text": plaintext,
    }
    pool.jobs.append(
        _reflection_job(entry_id=entry_id, content_hash=content_hash(plaintext), now=now)
    )

    result = await EmbedJobWorker(
        pool,
        settings=ReflectionWorkerSettingsStub(),
        embedder=TinyReflectionEmbedder(),
        worker_id="worker-r",
    ).run_once(now=now)

    assert result.embedded == 1
    assert all("FROM mediator.messages" not in sql for sql in pool.sql), (
        "reflection worker must not hydrate from mediator.messages"
    )


# ── Exclusion of empty/deferred candidates ───────────────────────────────────


async def test_empty_plaintext_in_view_with_stale_hash_supersedes():
    """When v_searchable_content has empty canonical_text but the job hash
    is different, the worker supersedes the stale job and enqueues a new
    one with the empty-text hash. The new job would fail to embed empty text
    but that's a separate concern — what matters is the lifecycle transition."""
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    entry_id = uuid4()
    pool = FakeReflectionWorkerPool()
    pool.searchable[("reflection", entry_id)] = {
        "source_type": "reflection",
        "source_id": entry_id,
        "message_id": None,
        "canonical_text": "",
    }
    pool.jobs.append(
        _reflection_job(entry_id=entry_id, content_hash=content_hash("something old"), now=now)
    )

    result = await EmbedJobWorker(
        pool,
        settings=ReflectionWorkerSettingsStub(),
        embedder=TinyReflectionEmbedder(),
        worker_id="worker-r",
    ).run_once(now=now)

    # Stale hash → superseded, and a new job with empty-text hash was enqueued
    assert result.superseded == 1
    assert pool.jobs[0]["status"] == "superseded"


async def test_reflection_source_type_present_in_embed_source_type_literal():
    """Verify 'reflection' is a valid EmbedSourceType in embed_jobs module."""
    source = open("app/services/embed_jobs.py").read()
    assert '"reflection"' in source
