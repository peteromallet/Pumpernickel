# Reflections M4 Release Evidence — Coverage Table

> **Plan:** m4-product-hardening-and-ship-20260720-0926  
> **Batch:** 2 (T2)  
> **Generated:** 2026-07-20  
> **Scope contract:** This table is the canonical surface map for all downstream M4 implementation tasks.  
> **Classification key:** `found` | `not_present` | `ambiguous` | `blocked_by_tooling` | `deferred`

---

## 1. Core Reflection Domain (`app/reflections/`)

| # | Surface | Status | Owner Subsystem | File Path(s) | Downstream Steps Affected |
|---|---------|--------|-----------------|-------------|--------------------------|
| 1 | Reflection message classifier | found | app/reflections | `app/reflections/classifier.py` (493 lines) | T5 (capture), T6 (integration), T9 (routing) |
| 2 | Session manager | found | app/reflections | `app/reflections/session_manager.py` (384 lines) | T5 (capture), T7 (store), T8 (finalization) |
| 3 | Bounded normalizer | found | app/reflections | `app/reflections/normalizer.py` (673 lines) | T10 (normalization bridge), T11 (entries) |
| 4 | Finalization engine | found | app/reflections | `app/reflections/finalization.py` (744 lines) | T8 (finalization worker), T9 (routing) |
| 5 | Derivation candidates | found | app/reflections | `app/reflections/derivation.py` (437 lines) | T12 (derivation), T13 (ledger), T14 (applier) |
| 6 | Derivation ledger | found | app/reflections | `app/reflections/derivation_ledger.py` (445 lines) | T13 (ledger), T16 (reconciliation) |
| 7 | Derivation applier | found | app/reflections | `app/reflections/derivation_applier.py` (773 lines) | T14 (applier), T15 (apply/retry) |
| 8 | Correction reconciliation | found | app/reflections | `app/reflections/reconciliation.py` (646 lines) | T16 (reconciliation), T17 (correction tests) |
| 9 | Period resolution | found | app/reflections | `app/reflections/periods.py` | T6 (integration) |
| 10 | Package init | found | app/reflections | `app/reflections/__init__.py` (7 lines) | Module boundary |

---

## 2. Reflection Services (`app/services/`)

| # | Surface | Status | Owner Subsystem | File Path(s) | Downstream Steps Affected |
|---|---------|--------|-----------------|-------------|--------------------------|
| 11 | ReflectionStore (session lifecycle) | found | app/services | `app/services/reflections.py` (2728 lines) | T7 (store), T8 (finalization), T10 (bridge), T11 (entries), T15 (retry) |
| 12 | Template registry + validators | found | app/services | `app/services/reflection_templates.py` (509 lines) | T11 (entries), T18 (migration validation) |
| 13 | Capture integration (inbound seam) | found | app/services | `app/services/reflections_integration.py` (250 lines) | T5 (capture), T6 (integration) |
| 14 | Finalization background worker | found | app/services | `app/services/reflections_finalization_worker.py` (400 lines) | T8 (finalization worker) |
| 15 | Normalization bridge | found | app/services | `app/services/reflections_normalization_bridge.py` (276 lines) | T10 (normalization bridge) |
| 16 | Reflection tool handlers | found | app/services | `app/services/tools/reflection_tools.py` (601 lines) | T19 (tool contracts), T20 (admin), T23 (redaction) |
| 17 | Tool registry (reflection tools) | found | app/services | `app/services/tools/registry.py` | T19 (tool contracts), T20 (admin) |
| 18 | Reflection embedding lifecycle | found | app/services | `app/services/message_embedding_lifecycle.py` | T21 (embedding), T26 (staging) |
| 19 | Embed jobs source type | found | app/services | `app/services/embed_jobs.py` | T21 (embedding) |
| 20 | Retrieval hydration | found | app/services | `app/services/retrieval.py` | T22 (retrieval), T23 (redaction) |
| 21 | Hot context reflections digest | found | app/services | `app/services/hot_context_solo.py` (1763 lines) | T22 (retrieval), T23 (redaction) |
| 22 | Turn plan reflection-aware routing | found | app/services | `app/services/turn_plan.py` | T9 (routing) |
| 23 | Weekly reflection seeding | found | app/services | `app/services/scheduled_job_handlers.py` | T24 (scheduler), T25 (eval fixtures) |
| 24 | Inbound weekly reflection seed | found | app/services | `app/services/inbound.py` | T6 (integration) |
| 25 | Onboarding reflection coach message | found | app/services | `app/services/onboarding_solo.py` | T6 (integration) |
| 26 | Prompts solo (reflection mirror) | found | app/services | `app/services/prompts_solo.py` | T6 (integration) |
| 27 | Embeddings transcript_reflection | found | app/services | `app/services/embeddings.py` | T21 (embedding) |
| 28 | Live artifacts transcript_reflection | found | app/services | `app/services/live/artifacts.py` | T26 (staging) |

---

## 3. Application Entry Points (`app/`)

| # | Surface | Status | Owner Subsystem | File Path(s) | Downstream Steps Affected |
|---|---------|--------|-----------------|-------------|--------------------------|
| 29 | Main lifespan (finalization worker, capture, weekly seed) | found | app | `app/main.py` (592 lines, lines 40-45, 226-257, 503-510) | T8 (finalization), T24 (scheduler), T26 (staging) |
| 30 | Config (reflection settings) | found | app | `app/config.py` (lines 216-218) | T8 (finalization), T26 (staging) |
| 31 | SuperPOM bot tool allowlist/denylist | found | app/bots | `app/bots/superpom.py` | T19 (tool contracts) |
| 32 | SuperPOM prompt profile | found | app/bots | `app/bots/prompts/profiles/superpom.py` | T19 (tool contracts), T25 (eval fixtures) |
| 33 | Coach bot reflection profile | found | app/bots | `app/bots/prompts/profiles/coach.py` | T25 (eval fixtures) |
| 34 | Mediator bot reflection prompt | found | app/bots | `app/bots/prompts/profiles/mediator.py` | T25 (eval fixtures) |

---

## 4. Migrations

| # | Surface | Status | Owner Subsystem | File Path(s) | Downstream Steps Affected |
|---|---------|--------|-----------------|-------------|--------------------------|
| 35 | Weekly reflection migration (0034) | found | migrations | `migrations/0034_weekly_reflection.sql` (29 lines) | T24 (scheduler) |
| 36 | Reflection foundation migration (0063 up) | found | migrations | `migrations/0063_reflection_foundation.sql` (408 lines) | T18 (migration validation), T26 (staging) |
| 37 | Reflection foundation migration (0063 down) | found | migrations | `migrations/0063_reflection_foundation.down.sql` | T18 (migration validation) |
| 38 | Reflection searchable content (0064 up) | found | migrations | `migrations/0064_reflections_searchable_content.sql` (504 lines) | T18 (migration validation), T22 (retrieval) |
| 39 | Reflection searchable content (0064 down) | found | migrations | `migrations/0064_reflections_searchable_content.down.sql` | T18 (migration validation) |
| 40 | Conversation artifacts transcript_reflection | found | migrations | `migrations/0051_conversation_artifacts.sql` | T26 (staging) |
| 41 | Embeddings unified index — transcript_reflection | found | migrations | `migrations/0058_content_embeddings_unified_index.sql` | T21 (embedding) |
| 42 | Deferred source types migration | found | migrations | `migrations/0059_content_embeddings_deferred_source_types.sql` | T21 (embedding) |

---

## 5. Tests (23 reflection-specific + 14 reflection-referencing)

| # | Surface | Status | Owner Subsystem | File Path(s) | Downstream Steps Affected |
|---|---------|--------|-----------------|-------------|--------------------------|
| 43 | Reflection services tests | found | tests | `tests/test_reflection_services.py` | Scoped baseline, T7 |
| 44 | Reflection tools tests | found | tests | `tests/test_reflection_tools.py` | Scoped baseline, T19 |
| 45 | Capture E2E tests | found | tests | `tests/test_reflections_capture_e2e.py` | Scoped baseline, T5 |
| 46 | Finalization worker tests | found | tests | `tests/test_reflections_finalization_worker.py` | Scoped baseline, T8 |
| 47 | Integration tests | found | tests | `tests/test_reflections_integration.py` | Scoped baseline, T6 |
| 48 | Derivation E2E tests | found | tests | `tests/test_reflections_derivation_e2e.py` | Scoped baseline, T12 |
| 49 | Hot context reflection tests | found | tests | `tests/test_hot_context_reflections.py` | Scoped baseline, T22 |
| 50 | Tool contract tests | found | tests | `tests/test_reflection_tool_contracts.py` | Scoped baseline, T19 |
| 51 | Classifier tests | found | tests | `tests/test_reflections_classifier.py` | T5 |
| 52 | Session manager tests | found | tests | `tests/test_reflections_session_manager.py` | T5 |
| 53 | Derivation applier tests | found | tests | `tests/test_reflections_derivation_applier.py` | T14 |
| 54 | Normalization bridge tests | found | tests | `tests/test_reflections_normalization_bridge.py` | T10 |
| 55 | Normalizer tests | found | tests | `tests/test_reflections_normalizer.py` | T10 |
| 56 | Periods tests | found | tests | `tests/test_reflections_periods.py` | T6 |
| 57 | Finalization tests | found | tests | `tests/test_reflections_finalization.py` | T8 |
| 58 | Reconciliation tests | found | tests | `tests/test_reflections_reconciliation.py` | T16 |
| 59 | Embed lifecycle tests | found | tests | `tests/test_reflection_embed_lifecycle.py` | T21 |
| 60 | Templates tests | found | tests | `tests/test_reflection_templates.py` | T11 |
| 61 | Derivation tests | found | tests | `tests/test_reflections_derivation.py` | T12 |
| 62 | Derivation ledger tests | found | tests | `tests/test_reflections_derivation_ledger.py` | T13 |
| 63 | Foundation migration tests | found | tests | `tests/test_reflection_foundation_migration.py` | T18 |
| 64 | Searchable migration tests | found | tests | `tests/test_migration_0064_reflections_searchable.py` | T18 |
| 65 | M3 integration tests | found | tests | `tests/test_m3_integration.py` | T6, T19, T22 |
| 66 | Retrieval tests (reflection) | found | tests | `tests/test_retrieval.py` | T22 |
| 67 | Scheduled jobs tests (weekly reflection) | found | tests | `tests/test_scheduled_jobs.py` | T24 |
| 68 | Pause/resume tests (weekly reflection) | found | tests | `tests/test_pause_resume.py` | T24 |
| 69 | SuperPOM prompt tests | found | tests | `tests/test_superpom_prompt.py` | T19 |
| 70 | SuperPOM per-bot corpus (reflection scenario) | found | tests | `tests/test_superpom_per_bot_corpus.py` | T25 |
| 71 | SuperPOM reflection routing tests | found | tests | `tests/test_superpom_reflection_routing.py` | T9 |
| 72 | Conftest fixtures (reflection) | found | tests | `tests/conftest.py` | All test tasks |
| 73 | Agentic privacy safety brief | found | tests | `tests/agentic/briefs/superpom-privacy-safety.md` | T23 (redaction) |
| 74 | Agentic decision mirror brief | found | tests | `tests/agentic/briefs/superpom-decision-mirror.md` | T19 |
| 75 | Agentic decision mirror scenario | found | tests | `tests/agentic/scenarios/superpom_decision_mirror.yaml` | T19 |
| 76 | Agentic privacy safety scenario | found | tests | `tests/agentic/scenarios/superpom_privacy_safety.yaml` | T23 (redaction) |
| 77 | Embeddings tests (transcript_reflection) | found | tests | `tests/test_embeddings.py` | T21 |
| 78 | Live artifacts migration tests | found | tests | `tests/test_live_artifacts_migration.py` | T26 |
| 79 | Withings tests (comment only) | found | tests | `tests/test_withings.py` | N/A (test comments only) |
| 80 | Consult perspective tests | found | tests | `tests/test_consult_perspective.py` | N/A (fixture data only) |

---

## 6. Evals

| # | Surface | Status | Owner Subsystem | File Path(s) | Downstream Steps Affected |
|---|---------|--------|-----------------|-------------|--------------------------|
| 81 | Clarify vague statement eval | found | evals | `evals/per_bot/superpom/01_clarify_vague_statement.md` | T25 (eval fixtures) |
| 82 | Compass-grounded reflection eval | found | evals | `evals/per_bot/superpom/04_compass_grounded_reflection.md` | T25 (eval fixtures) |
| 83 | Privacy suppression eval | found | evals | `evals/per_bot/superpom/07_privacy_suppression.md` | T23 (redaction), T25 (eval fixtures) |

---

## 7. Documentation

| # | Surface | Status | Owner Subsystem | File Path(s) | Downstream Steps Affected |
|---|---------|--------|-----------------|-------------|--------------------------|
| 84 | Reflection foundation handoff doc | found | docs | `docs/reflection-foundation-handoff.md` (467 lines) | T18 (migration validation), T26 (staging) |

---

## 8. Surfaces Verified as NOT PRESENT

These surfaces were explicitly searched and confirmed absent. They MUST NOT be assumed present by any downstream task.

| # | Surface | Status | Search Method | Evidence |
|---|---------|--------|---------------|----------|
| N1 | Reflection-specific admin endpoints | not_present | `rg reflection app/routers/admin.py` → 0 matches; full file read (421 lines) | No reflection admin pages, no reflection dashboard, no reflection operator endpoints |
| N2 | Reflection deletion logic | implemented_in_t13 | Historical audit: `rg reflection app/services/deletion.py` initially returned 0 matches; T13 added `cleanup_deleted_reflection_state()` plus delete-path wiring in `app/services/deletion.py`, `inbound.py`, `discord.py`, and `write_tools.py` | Deleted source messages now tombstone reflection payloads, retire derived targets, clear orphaned embedding/search state, and hide deleted-source sessions from admin/tool reads |
| N3 | Pending reflections view | not_present | `rg pending_reflection` repository-wide → 0 matches | No pending_reflections table, view, or query anywhere |
| N4 | Reflection-specific failure policy | not_present | `rg reflection app/services/failure_policy.py` → 0 matches | FailurePolicy is reflection-agnostic |
| N5 | Reflection recovery logic | not_present | `rg reflection app/services/recovery.py` → 0 matches | Recovery handles messages/inbound only |
| N6 | Reflection in staging.py | not_present | `rg reflection app/staging.py` → 0 matches | Staging has no reflection-specific validation |
| N7 | Reflection in inbound_queue.py | not_present | `rg reflection app/services/inbound_queue.py` → 0 matches | Queue handles messages only |
| N8 | Reflection in system_state.py | not_present | `rg reflect app/services/system_state.py` → 0 matches | System state is reflection-agnostic |
| N9 | Reflection in scripts/ | not_present | `rg reflection scripts/` → 0 matches | No scripts reference reflection |
| N10 | Reflection in Docker/deployment | not_present | `rg reflection` with file_glob `Dockerfile*` → 0 matches | No reflection-specific deployment config |
| N11 | Reflection in app/routers/ | not_present | `rg reflection app/routers/` → 0 matches | No router references reflection |
| N12 | Reflection retry service | not_present | No `app/services/retry.py` file exists | Retry logic lives within derivation_applier and reflections.py inline |
|| N13 | Reflection privacy/redaction module | **found (T4)** | Created `app/services/reflection_redaction.py` (301 lines) | Redaction helper wired into 11 log/exception sites across reflection_tools.py, reflections.py, reflections_finalization_worker.py |
|| N14 | Reflection admin/operator listing | **found (T6)** | `app/routers/admin.py` (lines 313-366), `app/services/reflections.py` (lines 2720-2818) | `/admin/reflections` GET endpoint with `admin_list_sessions()` query — exposes only redaction-safe metadata columns |

---

## 8a. T4 Redaction Wiring (2026-07-20)

The redaction helper ``app/services/reflection_redaction.py`` (301 lines) is now wired
into 11 diagnostic surfaces across 3 files.  Every exposed log, retry, or evidence
surface passes through ``redact_for_log_extra()``.

| # | Wired Surface | File | Line(s) | Preserved Fields | Redacted Fields |
|---|--------------|------|---------|-----------------|-----------------|
| R1 | search_reflections retrieval failed | reflection_tools.py | 317 | user_id, bot_id, topic_id, mode | query (pre-redacted in log msg) |
| R2 | _reconcile_after_correction probe failure | reflection_tools.py | 513 | superseded_entry_id, user_id | — |
| R3 | _reconcile_after_correction reconciliation failed | reflection_tools.py | 536 | superseded_entry_id, corrected_entry_id, user_id | — |
| R4 | correct_reflection failed | reflection_tools.py | 607 | user_id, supersedes_entry_id, bot_id | — |
| R5 | finalization worker tick failed | reflections_finalization_worker.py | 116 | worker_id | — |
| R6 | finalization worker session processing error | reflections_finalization_worker.py | 188 | worker_id, session_id | — |
| R7 | finalization worker normalize+create_entry failed | reflections_finalization_worker.py | 348 | session_id, user_id, bot_id, worker_id | — |
| R8 | mark_session_processed embed enqueue failed | reflections.py | 1067 | session_id | — |
| R9 | create_entry embed enqueue failed | reflections.py | 1638 | entry_id, session_id | — |
| R10 | correct_entry embed lifecycle failed | reflections.py | 2012 | entry_id, supersedes_entry_id | — |
| R11 | search_reflections query log | reflection_tools.py | 289 | user_id, bot_id, topic_id, mode, internals | args.query → `[REDACTED search query]` |

---

## 9. Ambiguous or Deferred

| # | Surface | Status | Rationale |
|---|---------|--------|-----------|
| — | (none) | — | All discovered surfaces classified as `found` or `not_present` |

---

## 10. Blocked by Tooling

| # | Surface | Status | Rationale |
|---|---------|--------|-----------|
| — | (none) | — | All searches completed successfully via `search_files` (ripgrep) |

---

## 8b. T7 Admin Operator Test Evidence (2026-07-20)

52 focused tests in ``tests/test_admin_reflections.py`` prove authorization, scope
boundaries, field-level non-sensitivity, and embedding/deletion indicators for the
``/admin/reflections`` operator endpoint.

### Exposed Field → Test Proof Map

Each of the 24 non-sensitive columns rendered by the admin endpoint has a
corresponding test that proves the field appears correctly and contains no
payload text.

| # | Exposed Field | Test Proof (test_admin_reflections.py) | Verifies |
|---|--------------|----------------------------------------|----------|
| 1 | `id` | `TestAdminReflectionsFieldPresence::test_session_id_rendered` | UUID rendered verbatim |
| 2 | `user_id` | `TestAdminReflectionsFieldPresence::test_user_id_rendered` | UUID rendered verbatim |
| 3 | `bot_id` | `TestAdminReflectionsFieldPresence::test_bot_id_rendered` | Bot string rendered |
| 4 | `topic_id` | `TestAdminReflectionsFieldPresence::test_topic_id_rendered` | UUID rendered verbatim |
| 5 | `template_key` | `TestAdminReflectionsScopeBoundaries::test_template_key_and_temporal_scope_visible` | Template key visible |
| 6 | `temporal_scope` | `TestAdminReflectionsScopeBoundaries::test_template_key_and_temporal_scope_visible` | Scope string visible |
| 7 | `phase` | `TestAdminReflectionsStuckActiveSessions::test_active_collecting_session_rendered` | Phase label visible |
| 8 | `status` | `TestAdminReflectionsStuckActiveSessions::test_multiple_sessions_different_statuses` | All 5 valid statuses visible |
| 9 | `classification_source` | `TestAdminReflectionsSensitivePayloadAbsence::test_classification_metadata_safe` | Source string visible, no payload |
| 10 | `classification_confidence` | `TestAdminReflectionsSensitivePayloadAbsence::test_classification_metadata_safe` | Float visible, no payload |
| 11 | `retry_count` | `TestAdminReflectionsStuckActiveSessions::test_stuck_session_failure_class_rendered` | Integer count visible |
| 12 | `failure_class` | `TestAdminReflectionsStuckActiveSessions::test_stuck_session_failure_class_rendered` | Failure class visible |
| 13 | `failure_reason` | `TestAdminReflectionsSensitivePayloadAbsence::test_failure_metadata_safe` | Reason visible, no payload |
| 14 | `last_error` | `TestAdminReflectionsSensitivePayloadAbsence::test_failure_metadata_safe` | Error string visible, no payload |
| 15 | `entry_count` | `TestAdminReflectionsEmbeddingDeletionIndicators::test_entry_count_rendered` | Integer count visible |
| 16 | `derivation_count` | `TestAdminReflectionsEmbeddingDeletionIndicators::test_derivation_count_rendered` | Integer count visible |
| 17 | `has_embeddable_entries` | `TestAdminReflectionsEmbeddingDeletionIndicators::test_embeddable_true_rendered` + `test_embeddable_false_rendered` | Boolean indicator visible, no content |
| 18 | `claimed_by` | `TestAdminReflectionsStuckActiveSessions::test_active_finalizing_session_rendered` | Worker ID visible |
| 19 | `created_at` | `TestAdminReflectionsFieldPresence::test_all_exposed_columns_in_html` | Column header present |
| 20 | `finalized_at` | `TestAdminReflectionsFieldPresence::test_all_exposed_columns_in_html` | Column header present |
| 21 | `processed_at` | `TestAdminReflectionsFieldPresence::test_all_exposed_columns_in_html` | Column header present |
| 22 | `abandoned_at` | `TestAdminReflectionsStuckActiveSessions::test_abandoned_session_rendered` | Column rendered for abandoned sessions |
| 23 | `idle_finalize_at` | `TestAdminReflectionsEmbeddingDeletionIndicators::test_idle_finalize_at_rendered` | Column rendered for collecting sessions |
| 24 | `updated_at` | `TestAdminReflectionsFieldPresence::test_all_exposed_columns_in_html` | Column header present |

### Sensitive Field Absence Proofs

Every known sensitive pattern is tested for absence in the admin HTML output.

| Sensitive Field | Absence Test | Result |
|----------------|-------------|--------|
| `plaintext_searchable` | `test_no_plaintext_searchable_in_output` | Sentinel payload NOT in HTML |
| `canonical_text` | `test_no_canonical_text_in_output` | Sentinel payload NOT in HTML |
| `summary` | `test_no_summary_in_output` | Sentinel payload NOT in HTML |
| `payload` | `test_no_payload_in_output` | Sentinel payload NOT in HTML |
| `transcript` | `test_no_transcript_in_output` | Sentinel payload NOT in HTML |
| `correction_note` | `test_no_correction_note_in_output` | Sentinel payload NOT in HTML |
| `source_text` | `test_no_source_text_in_output` | Sentinel payload NOT in HTML |
| `decrypted_body` | `test_no_decrypted_body_in_output` | Sentinel payload NOT in HTML |

### Authorization & Scope Tests

| Test | Verifies |
|------|----------|
| `test_requires_basic_auth` | 401 without credentials |
| `test_rejects_wrong_credentials` | 401 with wrong password |
| `test_rejects_wrong_username` | 401 with wrong username |
| `test_accepts_valid_credentials` | 200 with correct credentials |
| `test_distinct_users_visible` | Two different user_ids both visible |
| `test_distinct_topics_visible` | Two different topic_ids both visible |
| `test_same_user_different_bots_visible` | Same user, different bots both visible |
| `test_status_filter_invoked` | `?status=collecting` filters correctly |
| `test_status_filter_allows_all_when_absent` | No filter shows all statuses |

### admin_list_sessions Direct Function Tests

| Test | Verifies |
|------|----------|
| `test_returns_list_of_dicts` | Function returns correct type |
| `test_returns_all_expected_keys` | All 28 keys present in each row dict |
| `test_no_sensitive_columns_in_query` | No sensitive column names in SQL SELECT |
| `test_status_filter_validated` | Invalid status raises ValueError |
| `test_status_filter_accepted` | All 5 valid statuses accepted |
| `test_limit_applied` | LIMIT parameter passed to query |
| `test_has_embeddable_entries_is_boolean` | Embedding indicator is True/False, not content |
| `test_no_content_field_leaked` | `rs.content` not in SQL SELECT |

### Redaction Defense-in-Depth

| Test | Verifies |
|------|----------|
| `test_redaction_defense_in_depth_on_admin_row` | `redact_reflection_diagnostics()` correctly redacts injected sensitive fields while preserving safe fields — defense in depth even though SQL only SELECTs safe columns |
| `test_html_escaped_output` | HTML special characters in safe fields are escaped |
| `test_no_sensitive_field_names_in_html` | No sensitive field name appears as `<th>` column header |

---

## Failure Taxonomy Reconciliation (T8)

### Inventory

Two independent failure-class taxonomies exist in the codebase, serving different subsystems:

| Property | Reflection Sessions | Messages (failure_policy) |
|----------|-------------------|--------------------------|
| **Module** | `app/services/reflections.py` | `app/services/failure_policy.py` |
| **Storage** | `mediator.reflection_sessions.failure_class` | `mediator.messages.failure_class` |
| **CHECK constraint** | Migration 0063 | Migration 0046 |
| **Classes** | `retryable_processor`, `terminal_input`, `terminal_internal`, `stale_claim` | `retryable_pre_send`, `terminal_post_send`, `infra_bug`, `model_provider_bad_request`, `model_provider_timeout`, `tool_validation_recoverable`, `delivery_provider_failure` |
| **Total classes** | 4 | 7 |
| **Retry mechanism** | `retry_session()` transitions to `finalizing` | `FAILURE_POLICY` table + recovery sweep |
| **Consumer** | Reflection finalization worker, admin listing | Inbound queue, recovery sweep, `get_bot_actions_view` |

### Reconciliation Decision

**Keep both taxonomies independent. Do not merge, cross-map, or create a third taxonomy.**

Rationale:
1. The taxonomies model **different failure domains** — session processing vs. message delivery.
2. They are stored on **different tables** with different CHECK constraints.
3. They are consumed by **different subsystems** that should not couple to each other's failure model.
4. The admin listing surface (`GET /admin/reflections`) exposes `failure_class` from the reflection sessions table — this surface MUST use the 4-class reflection taxonomy, not the 7-class message taxonomy.
5. The message-level recovery sweep MUST NOT inspect `reflection_sessions.failure_class`.

### Documentation

- `app/services/reflections.py` lines 105-135: Taxonomy documentation comment block above `VALID_FAILURE_CLASSES`
- `app/services/failure_policy.py` lines 46-63: Cross-reference in `FailureClass` docstring
- This section in `docs/reflections_m4_release_evidence.md`

---

## Failure-Class Reconciliation Implementation (T9)

### Strategy Implemented

**Keep both taxonomies independent.** The implementation adds a deterministic reconciliation layer
(`app/services/failure_class_reconciliation.py`) that serves as the canonical source of truth for
both taxonomies and provides domain classification, validation, and display formatting.

### Files Changed

| File | Change |
|------|--------|
| `app/services/failure_class_reconciliation.py` | **NEW** — 240-line reconciliation module: canonical taxonomies, `classify_failure_domain()`, `validate_known_failure_class()`, `validate_reflection_failure_class()`, `format_failure_class()`, `get_failure_class_label()` |
| `app/services/reflections.py` | Updated: `VALID_FAILURE_CLASSES` now references `REFLECTION_FAILURE_CLASSES` from reconciliation module; `_validate_failure_class` is imported as `validate_reflection_failure_class` |
| `app/routers/admin.py` | Updated: imports `classify_failure_domain` and `format_failure_class`; adds `failure_class_domain` column; formats `failure_class` with `[R]`/`[M]` domain prefix for operator clarity |
| `tests/test_reflection_services.py` | Updated: `test_failure_class_invalid_raises` regex updated to match new error message `"invalid reflection failure_class"` |
| `docs/reflections_m4_release_evidence.md` | Updated: this section |

### Reconciliation Module API

| Function | Purpose |
|----------|---------|
| `classify_failure_domain(value)` | Returns `"reflection"`, `"message"`, or `None` |
| `validate_known_failure_class(value)` | Raises `ValueError` if value doesn't belong to either taxonomy |
| `validate_reflection_failure_class(value)` | Raises `ValueError` if value isn't a reflection class (used by `ReflectionStore.mark_session_failed()`) |
| `format_failure_class(value)` | Returns `"[R] retryable_processor"` or `"[M] infra_bug"` with domain tag |
| `get_failure_class_label(value)` | Returns human-readable label like `"Retryable (Processor)"` |

### Deterministic Resolution Guarantee

Every failure_class value emitted by the system now deterministically resolves through the
reconciliation layer:

1. **Storage**: Database CHECK constraints enforce domain-specific values at the storage layer
   - `mediator.reflection_sessions.failure_class` → only 4 reflection classes (migration 0063)
   - `mediator.messages.failure_class` → only 3 legacy message classes (migration 0046)
2. **Write-time validation**: `mark_session_failed()` validates via `validate_reflection_failure_class()`
   before writing
3. **Read-time classification**: `classify_failure_domain()` deterministically identifies the domain
   of any stored value
4. **Display formatting**: Admin listing uses `format_failure_class()` with domain tags so operators
   can disambiguate at a glance
5. **No third taxonomy**: Any attempt to emit an unrecognised value through
   `validate_known_failure_class()` raises `ValueError`

### Test Evidence

| Test file | Tests | Status |
|-----------|-------|--------|
| Reconciliation module inline tests (13 assertions) | Domain classification, validation, formatting, disjointness | All passed |
| `tests/test_reflection_services.py` | 204 passed (includes updated `test_failure_class_invalid_raises`) | All passed |
| `tests/test_admin_reflections.py` | 52 passed (domain tags within HTML still pass substring checks) | All passed |
| Scoped baseline (16 files, 518 tests) | All 518 passed, 49 skipped | All passed |

---

## Summary

- **Total surfaces discovered:** 85 (found) + 13 (not_present) = 98
- **Found:** 85 surfaces across app/reflections (10), app/services (18), app entry points (6), migrations (8), tests (38), evals (3), docs (1), routers (1 — N14)
- **Not present:** 13 surfaces (admin indexing — reclassified to N14 found by T6; deletion, pending_reflections, failure_policy, recovery, staging, inbound_queue, system_state, scripts, Docker, routers (non-reflection), retry service, privacy/redaction module — reclassified to found by T4)
- **Ambiguous:** 0
- **Blocked by tooling:** 0
- **Deferred:** 0
- **No tooling failures during discovery.**

### Downstream Task Impact Map

| Task | Depends On Surfaces |
|------|-------------------|
| T5 (capture) | #1, #2, #13, #43, #45, #51, #52 |
| T6 (integration) | #1, #2, #9, #13, #24, #25, #26, #47, #56, #65 |
| T7 (store) | #2, #11, #43 |
| T8 (finalization) | #2, #4, #11, #14, #29, #30, #46, #57 |
| T9 (routing) | #1, #4, #22, #71 |
| T10 (bridge) | #3, #11, #15, #54, #55 |
| T11 (entries) | #3, #11, #12, #60 |
| T12 (derivation) | #5, #48, #61 |
| T13 (deletion cleanup) | #2, #5, #6, #11, #18, #21, #27, #42, #49, #59, #62, #77 |
| T14 (applier) | #5, #7, #53 |
| T15 (retry) | #7, #11 |
| T16 (reconciliation) | #6, #8, #58 |
| T17 (correction tests) | #8 |
| T18 (migration validation) | #12, #36, #37, #38, #39, #63, #64, #84 |
| T19 (tool contracts) | #16, #17, #31, #32, #44, #50, #65, #69, #74, #75 |
| T20 (admin) | #16, #17, N14 (admin/reflections endpoint added by T6) |
| T21 (embedding) | #18, #19, #27, #41, #42, #59, #77 |
| T22 (retrieval) | #20, #21, #38, #49, #65, #66 |
|| T23 (redaction) | #16, #20, #21, #73, #76, #83, N13 (redaction helper created by T4) |
| T24 (scheduler) | #23, #29, #35, #67, #68 |
| T25 (eval fixtures) | #23, #32, #33, #34, #70, #81, #82, #83 |
| T26 (staging) | #18, #28, #29, #30, #36, #40, #78, #84 |

---

## Migration Validation (T17) — 2026-07-20

Step 6 validation used the M4 validator added in T16 so the run stayed aligned
with the repository's existing migration harness (`tests/test_reflection_foundation_migration.py`
and `tests/test_migration_0064_reflections_searchable.py`).

### Commands and Results

| Command | Exit | Result |
|---------|------|--------|
| `python scripts/validate_reflections_m4_migrations.py` | 0 | Static SQL checks passed and the harness ran `33 passed, 18 skipped in 0.08s`. |
| `python scripts/validate_reflections_m4_migrations.py --require-live` | 2 | Explicit live-gate failure recorded: `no safe TEST_DATABASE_URL, EVAL_DATABASE_URL, DATABASE_URL, or --database-url was provided`. |
| `python - <<'PY' ...` (env prerequisite probe) | 0 | `TEST_DATABASE_URL=missing`, `EVAL_DATABASE_URL=missing`, `DATABASE_URL=missing`. |
| `python - <<'PY' ...` (tool probe) | 0 | `psql=missing`, `docker=missing`. |

### Evidence Captured

- Static validator checks passed for all M4-specific migration assertions:
  - 0063 `failure_class` CHECK matches the approved reflection taxonomy.
  - 0063 still defines encrypted fields and retry/current indexes used by cleanup and recovery.
  - 0064 searchable-content reflection arm remains plaintext-only.
  - 0064 down migration deletes reflection embed rows before restoring legacy CHECK constraints.
- The harness re-ran both migration modules and passed every static migration test:
  - `tests/test_reflection_foundation_migration.py`: all 19 static tests passed.
  - `tests/test_migration_0064_reflections_searchable.py`: all 14 static tests passed.
- Down/rollback behavior is covered by the existing harness, but the live portions were skipped here because no safe scratch DSN was available:
  - 0063 live rollback tests were discovered and skipped, including `test_down_migration_removes_all_three_tables` and `test_down_migration_removes_all_six_policies`.
  - 0064 live scratch searchable-content tests were discovered and skipped because `TEST_DATABASE_URL` was unset.

### Execution Boundary

This environment did **not** execute a live scratch Postgres apply/rollback run on
2026-07-20. The exact blocker is missing safe scratch database configuration
(`TEST_DATABASE_URL`, `EVAL_DATABASE_URL`, and `DATABASE_URL` are all absent),
with no local `psql` or `docker` fallback available. This evidence therefore
certifies the static migration surface and records the precise live-validation
boundary without claiming live apply/rollback readiness.

---

## Evaluation Coverage Review (T18) — 2026-07-20

Step 7 discovery ran one focused pytest command across classifier, temporal
resolution, capture/integration, correction/retrieval, privacy, retry and
idempotency, deletion visibility, hot-context, prompt/routing, per-bot corpus,
and agentic evidence modules:

| Command | Exit | Result |
|---------|------|--------|
| `python -m pytest tests/test_reflections_classifier.py tests/test_reflections_periods.py tests/test_reflections_capture_e2e.py tests/test_reflections_integration.py tests/test_reflection_tools.py tests/test_reflection_redaction.py tests/test_reflection_m4_hardening.py tests/test_reflection_deletion_cleanup.py tests/test_hot_context_reflections.py tests/test_superpom_reflection_routing.py tests/test_superpom_per_bot_corpus.py tests/test_superpom_prompt.py tests/test_agentic_checks.py tests/test_agentic_evidence.py tests/test_agentic_scripted_tool.py tests/test_agentic_real_agent.py -v --tb=short` | 1 | `656 passed, 5 failed, 4 warnings in 1.80s`; all 5 failures match `baseline_test_failures` already recorded in `.megaplan/plans/m4-product-hardening-and-ship-20260720-0926/baseline.json`. |

### Stable behavior already covered

- **Classification + temporal scope:** `tests/test_reflections_classifier.py`,
  `tests/test_reflections_periods.py`, `tests/test_reflections_capture_e2e.py`,
  and `tests/test_reflections_integration.py` cover explicit reflection,
  implicit reflection, voice transcript parity, content-over-clock temporal
  scope resolution, and proactive/logistics negatives.
- **Correction + retrieval:** `tests/test_reflection_tools.py` covers
  `correct_reflection`, `search_reflections`, provenance, compact vs internals
  rendering, and non-reflection filtering.
- **Privacy:** `tests/test_reflection_redaction.py` covers log, admin, retry,
  eval-style, metric-label, and release-evidence redaction.
- **Retry/idempotency/deletion:** `tests/test_reflection_m4_hardening.py` and
  `tests/test_reflection_deletion_cleanup.py` cover repeated retry
  deduplication, restart recovery, failure-class consistency, deleted-source
  retrieval gating, admin gating, and hot-context exclusion.
- **Hot context:** `tests/test_hot_context_reflections.py` covers digest
  ordering, correction rendering, deletion gating, token-budget trimming, and
  payload-suppression rules.
- **Prompt + per-bot corpus:** `tests/test_superpom_prompt.py`,
  `tests/test_superpom_reflection_routing.py`, and
  `tests/test_superpom_per_bot_corpus.py` cover prompt safety, no proactive
  reflection invitations, correction routing, and the 7 existing SuperPOM
  markdown scenarios under `evals/per_bot/superpom/`.
- **Agentic evidence grading:** `tests/test_agentic_checks.py` and
  `tests/test_agentic_evidence.py` cover frozen evidence-pack grading rules for
  retrieval correctness, suppressed/deleted negatives, deepening before answer,
  and recoverable retry handling.

### Gaps ready for T19 (stable semantics; safe to fixture)

- No dedicated **per-bot or agentic fixture** currently isolates **implicit
  reflection** as a named evaluation scenario. The behavior is already stable
  in capture/integration tests, so adding a focused fixture will not encode
  provisional semantics.
- No dedicated corpus/agentic fixture currently exercises **voice-derived
  reflection**. Voice parity is already locked by
  `tests/test_reflections_capture_e2e.py` and
  `tests/test_reflections_integration.py`.
- No dedicated corpus/agentic fixture explicitly proves **temporal
  classification from content override instead of clock-only hints**. That
  behavior is already locked by `tests/test_reflections_classifier.py` and
  `tests/test_reflections_periods.py`.
- No named corpus/agentic fixture targets **no-proactive-outreach on the
  reflection path**. Prompt/routing/capture negatives already cover it, so a
  focused eval fixture is safe.
- **Retry deduplication, deletion visibility, and operator redaction** are now
  stable after T10 and T15, but their proof is still test-harness-level rather
  than evaluation-fixture-level.

### Deferred / tooling-blocked rather than semantic gaps

- The current environment still cannot run the **scripted-tool / real-agent /
  recorded-real** agentic execution path end to end. Two independent
  pre-existing blockers were observed:
  - `ModuleNotFoundError: No module named 'sisypy.schema'` from
    `tests/test_agentic_scripted_tool.py` and the runner path in
    `tests/test_agentic_real_agent.py`.
  - Missing required `Settings` inputs for real-agent evidence generation:
    `database_url`, `supabase_url`, `supabase_service_role_key`,
    `anthropic_api_key`, `openai_api_key`, `groq_api_key`,
    `whatsapp_verify_token`, and `admin_password`.
- Those 5 failing tests are already listed in `baseline_test_failures`, so T18
  treats them as **execution-boundary evidence** rather than new regressions or
  reasons to defer reflection retry/deletion/hot-context semantics themselves.

## Evaluation Fixture Additions (T19) — 2026-07-20

T19 added focused, file-backed SuperPOM eval fixtures only where the behavior
contract is already settled and the existing per-bot corpus can express it
without inventing new runtime semantics:

| Behavior | Fixture | Why this is stable now |
|---------|---------|------------------------|
| Explicit reflection | `evals/per_bot/superpom/04_compass_grounded_reflection.md` | Direct reflection ask + Compass grounding already finalized; T19 added the `explicit-reflection` tag so it is machine-checkable in corpus tests. |
| Implicit reflection | `evals/per_bot/superpom/08_implicit_pattern_reflection.md` | Classifier/integration tests already lock "pattern introspection without reflect wording" behavior. |
| Voice-derived reflection | `evals/per_bot/superpom/09_voice_checkpoint_reflection.md` | Capture/integration tests already lock transcript parity, so the fixture can focus on response behavior rather than transport details. |
| Temporal content override | `evals/per_bot/superpom/10_temporal_content_override.md` | Period/classifier tests already lock content-over-clock scope resolution; fixture keeps the month-scope expectation explicit. |
| Sensitive-content negative | `evals/per_bot/superpom/07_privacy_suppression.md` | Privacy suppression semantics were already stable; T19 added the `sensitive-content-negative` tag so the matrix is explicit. |
| No proactive outreach | `evals/per_bot/superpom/11_no_proactive_outreach.md` | Prompt/routing negatives already forbid unsolicited reflection invites, so this fixture isolates that user-facing behavior. |

### Deferred fixture targets

The remaining stable behaviors below stay **deferred at the eval-fixture layer**
for harness-fit reasons, not because the product semantics are unsettled:

- **Retry deduplication:** deferred from new eval fixtures because current eval
  harnesses do not exercise persisted retry/restart state transitions,
  interrupted finalization, and duplicate-side-effect recovery through a live
  reflection session store. The settled behavior remains covered by
  `tests/test_reflection_m4_hardening.py`.
- **Deletion visibility:** deferred from new eval fixtures because current eval
  fixtures do not seed deleted-source reflection rows plus downstream derived
  state, search tombstones, and hot-context/admin visibility in one runnable
  scenario. The settled behavior remains covered by
  `tests/test_reflection_deletion_cleanup.py` and
  `tests/test_hot_context_reflections.py`.
- **Operator redaction:** deferred from new eval fixtures because no existing
  eval runner captures `/admin/reflections` HTML or operator-only diagnostics
  as a scored evidence artifact. The settled behavior remains covered by
  `tests/test_admin_reflections.py` and `tests/test_reflection_redaction.py`.

These deferrals are about fixture/runtime shape only. They are not permission
to re-open behavior scope: the underlying retry, deletion, and redaction
contracts are already treated as finalized for release.

## Evaluation Rerun Evidence (T20) — 2026-07-20

After the T19 fixture additions landed, Step 7 reran the relevant
classification and SuperPOM evaluation suites once to capture concrete,
corpus-bounded evidence without quoting sensitive fixture payloads:

| Command | Exit | Result |
|---------|------|--------|
| `python -m pytest tests/test_reflections_classifier.py tests/test_reflections_periods.py tests/test_reflections_capture_e2e.py tests/test_reflections_integration.py tests/test_superpom_prompt.py tests/test_superpom_reflection_routing.py tests/test_superpom_per_bot_corpus.py tests/test_reflection_eval_fixtures.py -v --tb=short` | 0 | `386 passed, 4 warnings in 0.82s` |

### Concrete corpus results

- The rerun covered the rule-based classification surface (`classifier`,
  `periods`), capture/integration paths, SuperPOM prompt/routing constraints,
  the per-bot corpus loader, and the T19 fixture-matrix assertions in one
  foreground pass.
- `tests/test_superpom_per_bot_corpus.py` loaded **11** SuperPOM markdown
  scenarios from `evals/per_bot/superpom/` and confirmed that every scenario
  still carries tool assertions plus outbound assertions.
- The SuperPOM corpus still covers the required existing behavior matrix
  (`clarify`, `gentle-challenge`, `next-move`, `reflection`,
  `review-correction`, `completed-goals`, `privacy-suppression`,
  `shame-guardrail`, `generic-advice-avoid`, and `anti-pattern`) with all
  corpus scenarios tagged `superpom` and at least one scenario tagged
  `compass-first`.
- The finalized reflection fixture matrix remained present and well-formed for
  the six named release scenarios:
  `explicit-reflection`, `implicit-reflection`, `voice-derived`,
  `temporal-content-override`, `sensitive-content-negative`, and
  `no-proactive-outreach`.
- The rerun also reconfirmed the specific Step 7 classification qualities that
  T19 relies on: explicit and implicit reflection detection, voice-transcript
  parity, temporal scope overrides driven by message content rather than clock
  time alone, and no-proactive-outreach prompt/routing negatives.

### Non-sensitive observations

- The only warnings were four repeated `PytestRemovedIn10Warning` notices about
  class-scoped fixtures defined as instance methods in
  `tests/test_superpom_per_bot_corpus.py`. They did not change the pass/fail
  result on July 20, 2026, but they are a future-maintenance issue for pytest
  10 compatibility rather than new product-risk evidence.
- This section intentionally reports only file names, scenario names, tags, and
  test-module outcomes. It does not reproduce inbound fixture text, reflection
  bodies, transcripts, or operator-only payloads.

### Known limitations

- This evidence supports correctness only for the current **11-scenario
  SuperPOM corpus** plus the listed classification/routing test modules. It is
  not evidence of broad natural-language correctness across unseen phrasings,
  other bots, or production traffic.
- The rerun validates fixture structure and deterministic local behavior, not
  live model grading or full agentic execution. The separate T18 environment
  boundary remains: real-agent/scripted-tool execution is still blocked by the
  pre-existing `sisypy.schema` import gap and missing runtime secrets.
- Retry deduplication, deletion visibility, and operator redaction remain
  intentionally evidenced by their dedicated hardening tests rather than by new
  per-bot corpus fixtures, for the harness-fit reasons documented in T19.

## Staging Deployment (T23) — 2026-07-20

Step 9 required using the established repository deployment workflow to deploy
the complete build to staging without feature flags or partial activation when
the local checkout and credentials allowed it.

### Established deploy surface discovered in-repo

- `README.md` documents Railway as the application deployment target and says
  to deploy after adding the required environment variables.
- `.railwayignore` explicitly defines the upload boundary for `railway up`,
  confirming the intended local-directory deployment command.
- `railway.json` defines the deployed start command as
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- `docs/superpom-reflections-full-build.md` requires staging deployment before
  production deployment and explicitly forbids feature-flag or dormant-core
  rollout.
- `railway status` in this checkout resolved to the linked staging target:
  project `Veas`, environment `staging`, service `Veas-staging`.

### Non-sensitive deployment evidence

| Command | Exit | Output / finding |
|---------|------|------------------|
| `printf 'branch=%s\nsha=%s\n' "$(git branch --show-current)" "$(git rev-parse --short HEAD)"` | 0 | `branch=megaplan/m3-retrieval-context-and-20260720-0743` / `sha=dd0254b` |
| `railway whoami` | 0 | Logged in as `POM (peter@omalley.io)`. |
| `railway status` | 0 | Linked target resolved to project `Veas`, environment `staging`, service `Veas-staging`. |
| `python - <<'PY' ... railway variables ... PY` | 0 | Required staging/runtime variables were present: `ENV_NAME`, `DATABASE_URL`, `DIRECT_DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `ADMIN_PASSWORD`, `DATA_ENCRYPTION_KEY`; `total_vars=28`. |
| `railway up --detach --service Veas-staging --environment staging` | 0 | Uploaded the current checkout and queued deployment `c9c39099-500e-4047-9a50-88f1c2bda817`. |
| `python - <<'PY' ... railway deployment list ... PY` | 0 | Latest staging deployment `c9c39099-500e-4047-9a50-88f1c2bda817` reached `SUCCESS` at `2026-07-20T18:39:28.310Z`; image digest `sha256:d30536cfeec4403f9eb2a6a228662141300eeaffe12a873dada0183ed4a3cb93`; previous deployment `a656a945-550b-4c42-b5f4-7e9b5f9a79ce` moved to `REMOVED`. |
| `railway logs c9c39099-500e-4047-9a50-88f1c2bda817 --build --lines 400 \| rg "Nixpacks v1.41.0\|setup      \|install    \|build      \|start      \|vite v5.4.21\|✓ built in\|containerimage.digest"` | 0 | Build used `python311` + `nodejs_20`, ran `uv sync --no-dev --frozen`, `npm ci`, `vite build`, and exported container image digest `sha256:4f33335f3c6870796c76fb01ba5b1416c653143aec1477d13351de866728a88f`. |
| `railway logs --service Veas-staging --environment staging --lines 100 \| rg "Starting Container\|VEAS_VERSION=\|GET /health"` | 0 | Runtime logs showed the container start and Railway healthcheck `GET /health` returning `200 OK`. |
| `railway logs --service Veas-staging --environment staging --lines 100 --filter "@level:error"` | 0 | No error lines were returned immediately after cutover. |

Exact non-sensitive output captured for the decisive deployment commands:

```text
$ railway status
Project: Veas
Environment: staging
Service: Veas-staging
```

```text
$ python - <<'PY' ... railway variables ... PY
exit=0
ENV_NAME=SET
DATABASE_URL=SET
DIRECT_DATABASE_URL=SET
SUPABASE_URL=SET
SUPABASE_SERVICE_ROLE_KEY=SET
OPENAI_API_KEY=SET
ANTHROPIC_API_KEY=SET
ADMIN_PASSWORD=SET
DATA_ENCRYPTION_KEY=SET
total_vars=28
```

```text
$ railway up --detach --service Veas-staging --environment staging
Indexing...
Uploading...
  Build Logs: https://railway.com/project/7d0aae23-6f83-4107-85e2-aba051cdc26e/service/a96350d4-92f8-450f-a6d2-f7d2adcdb38a?id=c9c39099-500e-4047-9a50-88f1c2bda817&
```

```text
$ python - <<'PY' ... railway deployment list ... PY
{
  "service": "Veas-staging",
  "environment": "staging",
  "deployment_id": "c9c39099-500e-4047-9a50-88f1c2bda817",
  "status": "SUCCESS",
  "createdAt": "2026-07-20T18:39:28.310Z",
  "imageDigest": "sha256:d30536cfeec4403f9eb2a6a228662141300eeaffe12a873dada0183ed4a3cb93",
  "previous_deployment_id": "a656a945-550b-4c42-b5f4-7e9b5f9a79ce",
  "previous_status": "REMOVED"
}
```

```text
$ railway logs c9c39099-500e-4047-9a50-88f1c2bda817 --build --lines 400 | rg "Nixpacks v1.41.0|setup      |install    |build      |start      |vite v5.4.21|✓ built in|containerimage.digest"
╔══════════════════════════════ Nixpacks v1.41.0 ══════════════════════════════╗
║ setup      │ python311, nodejs_20                                            ║
║ install    │ python -m venv --copies /opt/venv && . /opt/venv/bin/activate   ║
║ build      │ cd web/live-voice && npm ci && npm run build                    ║
║ start      │ uvicorn app.main:app --host 0.0.0.0 --port $PORT                ║
vite v5.4.21 building for production...
✓ built in 1.59s
containerimage.digest: sha256:4f33335f3c6870796c76fb01ba5b1416c653143aec1477d13351de866728a88f
```

```text
$ railway logs --service Veas-staging --environment staging --lines 100 | rg "Starting Container|VEAS_VERSION=|GET /health"
Starting Container
VEAS_VERSION=hector-reliability-chain-deploy-2026-05-17
INFO:     100.64.0.2:34865 - "GET /health HTTP/1.1" 200 OK
```

### Result

- The established repository deploy path was Railway, and the deployment was
  executed through that existing workflow with
  `railway up --detach --service Veas-staging --environment staging`.
- Staging deployment `c9c39099-500e-4047-9a50-88f1c2bda817` completed
  successfully on July 20, 2026 for service `Veas-staging` in the `staging`
  environment.
- The complete build landed without introducing any feature flags, pilot-only
  activation, or partial rollout path; this matches the settled no-dormant-core
  shipping requirement in `docs/superpom-reflections-full-build.md`.
- Railway build and runtime evidence showed a successful `npm ci && npm run
  build`, exported image creation, container startup, and a passing `/health`
  check immediately after cutover.

## Staging Verification (T24) — 2026-07-20

Step 9 required exercising staging with one explicit text reflection, one
implicit text reflection, and one voice reflection, then proving provenance,
temporal classification, derivation/search, correction, retry/restart
recovery, deletion visibility, embedding coverage, operator diagnosis, and
the absence of proactive outreach with non-sensitive evidence only.

### Environment boundary used during verification

- `Veas-staging` exposed a Railway private domain only during this run; no
  public voice webhook/domain was available from the linked staging target.
- `SCHEDULER_ENABLED=false` in the staging environment on July 21, 2026, so
  the verification explicitly checked that no scheduled jobs or outbound
  messages were created for the synthetic staging user.
- Because the staging service had no public voice ingress, the voice path was
  exercised through the repository's lower-level `handle_voice()` harness
  against the staging database rather than through a public HTTP webhook.

### Commands and non-sensitive outcomes

| Command | Exit | Output / finding |
|---------|------|------------------|
| `python -m pytest tests/test_reflection_tools.py tests/test_reflection_templates.py tests/test_reflections_normalization_bridge.py tests/test_reflections_finalization_worker.py tests/test_reflection_tool_contracts.py tests/test_reflection_services.py tests/test_migration_0065_reflection_revision_leaf.py tests/test_migration_0064_reflections_searchable.py -v --tb=short` | 0 | `231 passed, 56 skipped in 0.64s`. |
| `railway up --detach --service Veas-staging --environment staging` | 0 | Uploaded the current checkout and queued deployment `5113bef6-1f98-4925-a0d1-a91fccf62efd`, which later reached `SUCCESS` with image digest `sha256:208d557d01453fe6b480e830d7124712d568f0a5264ec0ceac97b48de6a51589`. |
| `cat <<'PY' \| railway run --service Veas-staging --environment staging -- python -` | 0 | Integrated July 21 staging harness exercised explicit text, implicit text, and lower-level voice ingestion against staging and emitted a non-sensitive JSON summary proving correction/search, embedding coverage, retry/restart recovery, deletion visibility, operator diagnosis, and no proactive outreach. |

### Verified behaviors

- **Explicit text reflection:** processed successfully in staging with
  `template_key=freeform_reflection`, `classification_source=explicit_wording`,
  and `temporal_scope=day`.
- **Implicit text reflection:** a non-command, non-`reflection` message
  (`I noticed a pattern: I feel calmer...`) classified via
  `classification_source=message_semantics`; restart recovery from a
  finalized/no-entry session succeeded with `entries_before_recovery=0`,
  `entries_after_recovery=1`, and `temporal_scope=day`.
- **Voice reflection:** lower-level `handle_voice()` produced a transcript,
  capture/finalize succeeded, a synthetic `processing_failed` session appeared
  in operator/admin diagnostics with `failure_class=retryable_processor`, and
  retry/restart recovery completed to `processed` with `retry_count=1`.
- **Correction + search current revision:** exact reflection search for a
  correction-only token returned exactly one hit, and that hit pointed to the
  corrected entry ID (`before_delete_total=1`, `before_delete_hit_ids=[corrected_entry_id]`).
- **Embedding coverage on corrected revisions:** the corrected reflection entry
  had `1` row in `mediator.content_embeddings` before deletion, and that row
  was removed after deletion cleanup (`rows_after_delete=0`).
- **Deletion visibility:** after source-message deletion plus
  `cleanup_deleted_reflection_state()`, reflection list/search/admin visibility
  dropped to zero for the synthetic staging user (`visible_entries_after_delete=0`,
  `search_hits_after_delete=0`, `admin_rows_after_delete=0`).
- **No proactive outreach:** outbound-message count stayed `0 -> 0` and
  scheduled-job count stayed `0 -> 0` for the synthetic staging user.
- **Derivation provenance:** two derivations (`observation`, `memory`) were
  ledgered on staging for the explicit reflection, each with the synthetic
  source message ID in `supporting_message_ids`.
- **Searchable view state:** the staging database already exposed append-only
  leaf semantics on July 21, 2026 (`leaf_semantics_present=true`);
  `migration_0065_applied_during_probe=false`.

### Historical closure

- The July 20, 2026 correction-search / corrected-entry embedding blocker did
  not reproduce on the July 21, 2026 rerun. The current staging surface
  resolved the correction token to the corrected entry and materialized the
  corrected entry's embedding row before deletion.

### T24 verdict

- The staging/lower-level harness proved explicit, implicit, and voice capture,
  temporal classification, provenance, correction/search current-revision
  behavior, retry/restart recovery, deletion visibility, embedding coverage,
  operator diagnosis, and no-proactive-outreach behavior with non-sensitive
  evidence on July 21, 2026.
- T24 is **complete**. The prior July 20 staging blocker is closed by the
  July 21 rerun evidence above.

## Production Handoff (T25) - 2026-07-21

This section is the release handoff boundary for M4. It consolidates the
evidence already recorded above, names the remaining execution boundary
explicitly, and does **not** authorize a production mutation by itself.

### Release evidence summary

| Gate | Latest evidence in this run | Status |
|------|-----------------------------|--------|
| Deletion / retention cleanup | `docs/reflections_m4_deletion_audit.md` reports no remaining staging-blocking rows as of `2026-07-20`. | Ready |
| Focused reflection baseline | T21 recorded `604 passed, 49 skipped, 1 warning` with no failures beyond the recorded baseline. | Ready |
| Repo verification gate | T22 recorded `python scripts/lint_inserts.py` passing, `bash scripts/build_live_ui.sh` passing, expected `lint_artifact_reads.py` clean-head findings, and a single full-suite run with no new failures beyond `baseline_test_failures`. | Ready with known baseline debt |
| Evaluation evidence | T20 reran the Step 7 corpus/tests at `386 passed, 4 warnings` and documented corpus-limited claims only. | Ready |
| Migration validation | T17 static validator passed; live scratch apply/rollback remained unavailable because no safe scratch DSN was present on July 20, 2026. | Boundary remains |
| Staging deployment | T23 deployed `Veas-staging` successfully via Railway (`c9c39099-500e-4047-9a50-88f1c2bda817`). | Ready |
| Staging behavior proof | T24 reran staging on July 21, 2026 and closed the prior blocker with explicit, implicit, and lower-level voice verification plus correction/search, recovery, deletion, and no-proactive-outreach evidence. | Ready |

### Production authority breakpoint

- The repository-established deploy path is Railway:
  `railway up --detach --service <service> --environment <environment>`.
- `docs/superpom-reflections-full-build.md` sets the required order:
  staging proof first, then production migration + production deploy + post-deploy verification.
- This handoff stops at the **production authority breakpoint**. Do **not**
  run production migrations or a production deploy until all of the following
  are true:
  1. A production operator explicitly authorizes the production Railway target
     and migration window.
  2. The missing live scratch Postgres validation boundary from T17 is either
     cleared with a safe DSN run or accepted in writing by the production
     authority.
  3. The release operator is prepared to run the repo's required post-deploy
     checks for health, worker processing, reflection creation, derivations,
     retrieval, admin visibility, and absence of duplicate or cross-scope
     writes.

If any item above is unresolved, production execution is blocked and this M4
artifact remains a handoff package rather than a release authorization.

### Operational handoff

- Primary operator evidence: this document, `docs/reflections_m4_deletion_audit.md`,
  `.megaplan/plans/m4-product-hardening-and-ship-20260720-0926/baseline.json`,
  and the staged deployment evidence under T23/T24.
- Known residual limitations that must stay attached to any production decision:
  live scratch Postgres migration apply/rollback was not executed in this run;
  evaluation claims remain limited to the documented corpus and focused test
  selectors; production deployment identifiers are not present because this
  batch intentionally stopped before a production mutation.
- Verification discipline for the next stage: rely on the authoritative
  post-execute harness for regression judgment and do not reinterpret the
  recorded baseline failures as M4 regressions unless they move outside
  `baseline_test_failures`.
