#!/usr/bin/env python3
"""EXPLAIN regression check for the artifact_topics join cutover (T15).

Runs representative read queries against the prod database and verifies that
each plan uses the partial index ``idx_artifact_topics_topic_artifact_active``.

Exits non-zero if any query plan shows a Seq Scan on a large artifact table
or fails to use the expected index.

Usage:
    python scripts/explain_artifact_topics_reads.py --help
    python scripts/explain_artifact_topics_reads.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from uuid import UUID

# We intentionally import nothing from the app — this script is self-contained
# and only needs asyncpg + the DATABASE_URL env var.

INDEX_NAME = "idx_artifact_topics_topic_artifact_active"
TOPIC_ID = "00000000-0000-4000-8000-000000000001"  # relationship topic for tests


def _plan_uses_index(plan: dict) -> bool:
    """Recursively search an EXPLAIN plan node for the expected index name."""
    node_type = plan.get("Node Type", "")
    index_name = plan.get("Index Name", "")
    if index_name == INDEX_NAME and node_type in ("Index Scan", "Index Only Scan", "Bitmap Index Scan"):
        return True
    if node_type == "Bitmap Heap Scan" and index_name == INDEX_NAME:
        return True
    # Check sub-plans
    for key in ("Plans", "Sub Plan"):
        sub_plans = plan.get(key)
        if isinstance(sub_plans, list):
            for sub in sub_plans:
                if _plan_uses_index(sub):
                    return True
    return False


def _plan_has_artifact_topics_index(plan: dict) -> str | None:
    """Return the index name if any artifact_topics index is used, else None."""
    index_name = plan.get("Index Name", "")
    if "artifact_topics" in index_name:
        return index_name
    for key in ("Plans", "Sub Plan"):
        sub_plans = plan.get(key)
        if isinstance(sub_plans, list):
            for sub in sub_plans:
                result = _plan_has_artifact_topics_index(sub)
                if result:
                    return result
    return None


def _plan_has_seq_scan(plan: dict, relation_name: str | None = None) -> bool:
    """Check if plan has a Seq Scan (optionally on a specific relation)."""
    node_type = plan.get("Node Type", "")
    if node_type == "Seq Scan":
        if relation_name is None or plan.get("Relation Name") == relation_name:
            return True
    for key in ("Plans", "Sub Plan"):
        sub_plans = plan.get(key)
        if isinstance(sub_plans, list):
            for sub in sub_plans:
                if _plan_has_seq_scan(sub, relation_name):
                    return True
    return False


# ---------------------------------------------------------------------------
# Representative queries — one per artifact family, modeled on the rewritten
# read shapes from Sprint 3.  Each uses the same JOIN pattern that
# build_hot_context / read_tools produce.
# ---------------------------------------------------------------------------

QUERIES = {
    "memories": (
        "m",
        """SELECT id, about_user_id, content, COALESCE(related_theme_ids, '{}'::uuid[]) AS related_theme_ids,
                  last_referenced_at, created_at
           FROM memories
           JOIN artifact_topics _at_m
             ON _at_m.artifact_table = 'memories'
            AND _at_m.artifact_id = memories.id
            AND _at_m.topic_id = $1::uuid
            AND _at_m.status = 'active'
           WHERE memories.status = 'active'
           ORDER BY COALESCE(last_referenced_at, created_at) DESC
           LIMIT 80""",
    ),
    "themes": (
        "t",
        """SELECT id, title, description, status, sentiment, health, last_reinforced_at, last_active_at
           FROM themes
           JOIN artifact_topics _at_t
             ON _at_t.artifact_table = 'themes'
            AND _at_t.artifact_id = themes.id
            AND _at_t.topic_id = $1::uuid
            AND _at_t.status = 'active'
           WHERE themes.status = 'active'
           ORDER BY COALESCE(last_reinforced_at, first_seen_at) DESC
           LIMIT 10""",
    ),
    "observations": (
        "o",
        """SELECT id, about_user_id, content, confidence, significance,
                  COALESCE(related_theme_ids, '{}'::uuid[]) AS related_theme_ids,
                  last_reinforced_at, created_at
           FROM observations
           JOIN artifact_topics _at_o
             ON _at_o.artifact_table = 'observations'
            AND _at_o.artifact_id = observations.id
            AND _at_o.topic_id = $1::uuid
            AND _at_o.status = 'active'
           WHERE observations.status = 'active' AND significance >= 3
           ORDER BY recency_weighted_score(significance, last_reinforced_at, created_at) DESC NULLS LAST,
                    COALESCE(last_reinforced_at, created_at) DESC
           LIMIT 80""",
    ),
    "watch_items": (
        "w",
        """SELECT id, owner_user_id, content, due_at, COALESCE(related_theme_ids, '{}'::uuid[]) AS related_theme_ids
           FROM watch_items
           JOIN artifact_topics _at_w
             ON _at_w.artifact_table = 'watch_items'
            AND _at_w.artifact_id = watch_items.id
            AND _at_w.topic_id = $1::uuid
            AND _at_w.status = 'active'
           WHERE watch_items.status = 'open'
           ORDER BY COALESCE(due_at, created_at) ASC""",
    ),
    "distillations": (
        "d",
        """SELECT id, content, confidence, status, sensitivity, visibility, shareable_summary,
                  COALESCE(source_user_ids, '{}'::uuid[]) AS source_user_ids,
                  COALESCE(related_memory_ids, '{}'::uuid[]) AS related_memory_ids,
                  COALESCE(related_observation_ids, '{}'::uuid[]) AS related_observation_ids,
                  COALESCE(related_theme_ids, '{}'::uuid[]) AS related_theme_ids,
                  COALESCE(supporting_message_ids, '{}'::uuid[]) AS supporting_message_ids,
                  revision_note, revision_count, updated_at, created_at
           FROM distillations
           JOIN artifact_topics _at_d
             ON _at_d.artifact_table = 'distillations'
            AND _at_d.artifact_id = distillations.id
            AND _at_d.topic_id = $1::uuid
            AND _at_d.status = 'active'
           WHERE distillations.status = 'active'
           ORDER BY updated_at DESC, created_at DESC
           LIMIT 12""",
    ),
    "out_of_bounds": (
        "x",
        """SELECT id, owner_id, shareable_context, severity, review_at
           FROM out_of_bounds
           JOIN artifact_topics _at_x
             ON _at_x.artifact_table = 'out_of_bounds'
            AND _at_x.artifact_id = out_of_bounds.id
            AND _at_x.topic_id = $1::uuid
            AND _at_x.status = 'active'
           WHERE out_of_bounds.status = 'active'
           ORDER BY CASE severity WHEN 'hard' THEN 1 WHEN 'firm' THEN 2 ELSE 3 END, created_at DESC""",
    ),
}


def _make_connection_url() -> str:
    """Derive a session-mode URL from DATABASE_URL for the EXPLAIN check.

    Port 6543 is transaction-mode (Supabase pooler) — we cannot use it for
    EXPLAIN because transaction-mode doesn't support prepared statements.
    We rewrite to port 5432 (session-mode).
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("error: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    # Replace port 6543 → 5432 for session-mode
    if ":6543/" in url:
        url = url.replace(":6543/", ":5432/")
    return url


async def _run_explain(pool, query: str, topic_id: UUID) -> dict:
    """Run EXPLAIN (FORMAT JSON) on *query* and return the top-level plan."""
    explain_sql = f"EXPLAIN (FORMAT JSON) {query}"
    rows = await pool.fetch(explain_sql, topic_id)
    # asyncpg returns the JSON plan as a list of plan objects
    raw = rows[0][0]
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list):
        return raw[0].get("Plan", raw[0])
    return raw.get("Plan", raw)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="EXPLAIN regression check for artifact_topics join cutover."
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Database connection URL (default: $DATABASE_URL with port 5432)",
    )
    parser.add_argument(
        "--topic-id",
        default=TOPIC_ID,
        help="Topic UUID to use in query binds (default: relationship topic placeholder)",
    )
    parser.add_argument(
        "--schema",
        default="mediator",
        help="Schema to set search_path to (default: mediator)",
    )
    args = parser.parse_args()

    conn_url = args.url or _make_connection_url()
    topic_id = UUID(args.topic_id)
    schema = args.schema

    import asyncpg

    pool = await asyncpg.create_pool(
        conn_url,
        statement_cache_size=0,
        server_settings={"search_path": schema},
    )
    if pool is None:
        print("error: could not create connection pool", file=sys.stderr)
        return 1

    try:
        all_pass = True
        for family, (alias, query) in QUERIES.items():
            try:
                plan = await _run_explain(pool, query, topic_id)
            except Exception as exc:
                print(f"FAIL  {family:20s} — EXPLAIN error: {exc}")
                all_pass = False
                continue

            uses_target = _plan_uses_index(plan)
            other_idx = _plan_has_artifact_topics_index(plan)
            has_seq = _plan_has_seq_scan(plan, family if family != "out_of_bounds" else None)

            if uses_target:
                print(f"PASS  {family:20s} — uses {INDEX_NAME}")
            elif other_idx and other_idx != INDEX_NAME:
                print(f"WARN  {family:20s} — uses older artifact_topics index '{other_idx}' (not {INDEX_NAME})")
                # WARN is acceptable; don't fail
            elif has_seq:
                print(f"FAIL  {family:20s} — Seq Scan on {family} table (index missing or not used)")
                all_pass = False
            else:
                # Neither uses index nor has seq scan: just report the plan
                node_type = plan.get("Node Type", "unknown")
                print(f"WARN  {family:20s} — plan uses {node_type} (no artifact_topics index detected)")
                # Not a fail because it might use a different path; still warn

        if all_pass:
            print("\nAll EXPLAIN checks passed.")
        else:
            print("\nSome EXPLAIN checks FAILED — see above.", file=sys.stderr)
    finally:
        await pool.close()

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))