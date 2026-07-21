# Release Readiness Proof-Map

**Purpose:** Trace every settled contract invariant to an automated test selector or an acknowledged
milestone gap. This is the evidence ledger for rollout-readiness decisions. It **separates**
automated (CI-verifiable) evidence from pending live validation that requires vendor approval,
production credentials, or operator action.

**Status:** Finalized — complete automated evidence map with metric/alert names, privacy guardrails, and explicit pending human prerequisites. **Live rollout is not completed and is not claimed.**

**Last updated:** 2026-07-21

---

## Table of Contents

1. [Withings Provider Contract](#1-withings-provider-contract)
2. [Weight & Sleep Read Model Contract](#2-weight--sleep-read-model-contract)
3. [Workout Projection Contract](#3-workout-projection-contract)
4. [Observability — Metrics & Alerts](#4-observability--metrics--alerts)
5. [Privacy Guardrail Evidence](#5-privacy-guardrail-evidence)
6. [Admin Diagnostics Surface](#6-admin-diagnostics-surface)
7. [Lifecycle & Data Portability](#7-lifecycle--data-portability)
8. [Synthetic Canary Evidence](#8-synthetic-canary-evidence)
9. [Failure Drill Coverage](#9-failure-drill-coverage)
10. [Weekly Digest Generator](#10-weekly-digest-generator)
11. [Config & Rollout Guard Evidence](#11-config--rollout-guard-evidence)
4. [Gap Summary](#4-gap-summary)
5. [Rollout Readiness Declaration](#13-rollout-readiness-declaration)

---

## 1. Withings Provider Contract

**Source:** `docs/health/withings-provider-contract.md` (status: finalized M1 handoff contract)

### 1.1 Provider Interface Shape

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| `HealthSyncProvider` has exactly 4 methods: `exchange_code`, `refresh_token`, `fetch_changes`, `revoke` | §3 | `test_provider_protocol_stays_minimal_and_withings_shaped` | ✅ Automated |
| Each method has keyword-only parameters matching spec | §3 | Same selector above | ✅ Automated |
| Return types match spec (`HealthOAuthTokens`, `HealthFetchResult`, `None`) | §3 | Same selector above | ✅ Automated |

### 1.2 Capability Map

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Provider slug is `"withings"` | §3 | `test_withings_capabilities_are_category_driven` | ✅ Automated |
| 3 resource types: measurement, workout, sleep | §3 | Same selector above | ✅ Automated |
| Category-to-scope map: measurement→`user.metrics`, workout→`user.activity`, sleep→`user.activity` | §3 | Same selector above | ✅ Automated |

### 1.3 Cursor State

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Cursor JSON shape: `resource_type`, `last_modified`, `page_offset`, `etag` | §5 | `test_cursor_state_round_trip_uses_expected_json_shape` | ✅ Automated |
| `overlap_window` defaults to 48 hours | §5 | Same selector above (`DEFAULT_CURSOR_OVERLAP` assertion) | ✅ Automated |
| Round-trip serialization via `to_state`/`from_state` | §5 | Same selector above | ✅ Automated |

### 1.4 External Key Derivation

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Fallback key is deterministic regardless of JSON key order | §6 | `test_build_fallback_external_id_is_deterministic` | ✅ Automated |
| Native `external_id` wins over fallback (whitespace-stripped) | §6 | `test_resolve_external_id_prefers_native_identifier` | ✅ Automated |

### 1.5 OAuth & Connection Lifecycle

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Connection states: `active`, `reauth_required`, `disconnected`, `deleted` | §4 | (covered in `test_health_devices_auth.py` and `test_health.py`) | ✅ Automated |
| Encrypted token persistence after callback | §4 | (covered in `test_health_tokens.py`) | ✅ Automated |
| Refresh-token rotation uses optimistic locking | §4 | (covered in `test_health_tokens.py`) | ✅ Automated |

### 1.6 Notification & Dirty-Category Queueing

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Webhook intake is queue-only (no inline fetch) | §8 | (covered in `test_health_notifications.py`) | ✅ Automated |
| Category map: 1→measurement, 16→workout, 44/50/51/52→sleep | §8 | (covered in `test_health_notifications.py`) | ✅ Automated |
| Deduplication by SHA-256 of canonicalized form fields | §8 | (covered in `test_health_notifications.py`) | ✅ Automated |
| Unknown connections logged as ignored receipts | §8 | (covered in `test_health_notifications.py`) | ✅ Automated |

### 1.7 End-to-End Sync

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Fake provider e2e covers backfill, dirty-category, overlap, tombstone, reconcile, cleanup | — | `test_fake_provider_e2e_covers_backfill_dirty_overlap_tombstone_reconcile_and_cleanup` | ✅ Automated |

### 1.8 Operator Prerequisites (Live Validation Required)

| Invariant | Contract § | Test Selector | Evidence Status |
|---|---|---|---|
| `DATA_ENCRYPTION_KEY` provisioned in production | §13 | — | ❌ **Gap — operator action** |
| `WITHINGS_CLIENT_ID` and `WITHINGS_CLIENT_SECRET` provisioned | §13 | — | ❌ **Gap — operator action** |
| HTTPS callback URL registered at `/api/health/devices/withings/oauth/callback` | §13 | — | ❌ **Gap — operator action** |
| HTTPS notification endpoint at `/api/health/devices/withings/notifications` | §13 | — | ❌ **Gap — operator action** |
| Live Withings API entitlement & vendor approval obtained | §13 | — | ❌ **Gap — vendor action** |
| Notification subscribe flow verified against approved app | §13 | — | ❌ **Gap — live validation** |
| Health flags off until credentials are ready (`HEALTH_SYNC_ENABLED`, per-category flags) | §13 | — | ❌ **Gap — operator action** |

---

## 2. Weight & Sleep Read Model Contract

**Source:** `docs/health/weight-sleep-read-model-contract.md` (status: settled post-implementation)

### 2.1 Measurement Decoding

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| `decode_withings_value(value, unit)` = value × 10^unit | §2.1 | `test_decode_withings_value` (parametrized: weight, fat_ratio, muscle_mass, bone_mass, edge cases) | ✅ Automated |
| Returns `float` type | §2.1 | `test_decode_withings_value_float` | ✅ Automated |

### 2.2 Metric Map

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Type 1 → `weight` / `kg` | §3 | `test_metric_map_contains_weight` | ✅ Automated |
| Type 6 → `fat_ratio` / `percent` | §3 | `test_metric_map_contains_type_6_as_fat_ratio` | ✅ Automated |
| Type 8 → `fat_mass` / `kg` | §3 | `test_metric_map_contains_fat_mass` | ✅ Automated |
| Type 76 → `muscle_mass` / `kg` | §3 | `test_metric_map_contains_muscle_mass` | ✅ Automated |
| Type 88 → `bone_mass` / `kg` | §3 | `test_metric_map_contains_bone_mass` | ✅ Automated |
| Unmapped types (e.g., 999) return `None` | §3 | `test_metric_info_returns_none_for_unmapped_type` | ✅ Automated |
| Mapped types return `(metric, unit)` tuple | §3 | `test_metric_info_returns_tuple_for_mapped_types` | ✅ Automated |

### 2.3 Measurement Normalization (Group)

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| `normalize_measure_group` decodes weight + fat_ratio from fixture data | §2.1 | `test_normalize_measure_group_decodes_weight_and_fat_ratio` | ✅ Automated |
| Unmapped types silently skipped (no row) | §2.2 | `test_normalize_measure_group_skips_unmapped_types` | ✅ Automated |
| Empty measures list produces empty result | §2.2 | `test_normalize_measure_group_empty_measures` | ✅ Automated |
| Attribution propagated as copy to all rows | §6.1 | `test_normalize_measure_group_attribution_propagated_to_all_rows`, `test_normalize_measure_group_attribution_is_a_copy` | ✅ Automated |
| Null optional fields propagated as `None` | §5.1 | `test_normalize_measure_group_none_optionals` | ✅ Automated |
| Missing `value` or `unit` raises `KeyError` | §2.2 | — | ⚠️ **Gap — no explicit test found** |

### 2.4 Sleep Normalization (Summary)

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| `normalize_sleep_summary` extracts basic fields (started_at, ended_at, etc.) | §11.3 | `test_normalize_sleep_summary_extracts_basic_fields` | ✅ Automated |
| `local_sleep_date` derived from wake/end time | §9.1 | `test_normalize_sleep_summary_local_sleep_date_from_wake_time` | ✅ Automated |
| Cross-midnight: wake time determines date | §9.4 | `test_normalize_sleep_summary_cross_midnight_utc`, `test_normalize_sleep_summary_cross_midnight_europe` | ✅ Automated |
| DST spring forward/fall back handled correctly | §9.3 | `test_normalize_sleep_summary_dst_winter`, `test_normalize_sleep_summary_dst_summer` | ✅ Automated |
| Partial completeness state | §7.3 | `test_normalize_sleep_summary_partial_completeness` | ✅ Automated |
| Complete sleep summary | §11.3 | `test_normalize_sleep_summary_complete` | ✅ Automated |
| Revision identification | §7.3 | `test_normalize_sleep_summary_revised`, `test_normalize_sleep_summary_revised_count_one_is_complete`, `test_normalize_sleep_summary_not_completed_never_revised` | ✅ Automated |
| Detail records excluded (only summaries normalized) | §1 | `test_normalize_sleep_summary_detail_record_excluded` | ✅ Automated |
| Non-sleep records excluded | §1 | `test_normalize_sleep_summary_non_sleep_record_excluded` | ✅ Automated |
| Null timezone → `local_sleep_date` falls back to UTC | §9.3 | `test_normalize_sleep_summary_null_timezone` | ✅ Automated |
| Invalid timezone → UTC fallback | §5.3 | `test_normalize_sleep_summary_invalid_timezone_fallback` | ✅ Automated |
| Missing optional fields → `None` | §5.2 | `test_normalize_sleep_summary_optional_null_handling` | ✅ Automated |
| Missing `starts_at` → returns `None` | §2.2 | `test_normalize_sleep_summary_missing_starts_at` | ✅ Automated |
| Attribution copied (not shared reference) | §6.1 | `test_normalize_sleep_summary_attribution_is_copy` | ✅ Automated |
| Empty device ID/model → `None` | §5.1/§5.2 | `test_normalize_sleep_summary_empty_device_id_becomes_none`, `test_normalize_sleep_summary_empty_device_model_becomes_none` | ✅ Automated |

### 2.5 Timezone Utilities

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| `resolve_timezone` valid IANA name → `ZoneInfo` | §5.3 | `test_resolve_timezone_valid`, `test_resolve_timezone_utc` | ✅ Automated |
| `resolve_timezone` None/empty/whitespace → `None` | §5.3 | `test_resolve_timezone_none`, `test_resolve_timezone_empty_string`, `test_resolve_timezone_whitespace` | ✅ Automated |
| `resolve_timezone` invalid/garbage → `None` | §5.3 | `test_resolve_timezone_invalid_name`, `test_resolve_timezone_garbage` | ✅ Automated |
| `calculate_offset_seconds` DST-aware | §9.3 | `test_calculate_offset_seconds_nyc_winter`, `test_calculate_offset_seconds_nyc_summer`, `test_calculate_offset_seconds_dst_spring_forward`, `test_calculate_offset_seconds_dst_fall_back`, `test_calculate_offset_seconds_utc` | ✅ Automated |
| `calculate_offset_seconds` returns `None` for missing/invalid zone | §9.3 | `test_calculate_offset_seconds_null_on_none_zone`, `test_calculate_offset_seconds_null_on_empty_zone`, `test_calculate_offset_seconds_null_on_invalid_zone` | ✅ Automated |

### 2.6 Repository — Replace & Delete Semantics

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| `replace_normalized_measurements` inserts rows | §7.1 | `test_replace_normalized_measurements_inserts_rows` | ✅ Automated |
| `replace_normalized_measurements` deletes existing rows first | §7.1 | `test_replace_normalized_measurements_deletes_existing` | ✅ Automated |
| Empty list deletes all without inserting | §7.1 | `test_replace_normalized_measurements_empty_list_deletes_all` | ✅ Automated |
| `delete_normalized_measurements` removes all for source record | §8.1 | `test_delete_normalized_measurements_removes_all` | ✅ Automated |
| Operations scoped by `user_id` and `connection_id` | §6.2 | `test_measurement_ops_scoped_by_user_and_connection` | ✅ Automated |
| Wrong user/connection not affected | §6.2 | `test_measurement_ops_wrong_user_not_deleted`, `test_measurement_ops_wrong_connection_not_deleted` | ✅ Automated |
| Attribution preserved on replace | §6.1 | `test_measurement_replace_attribution_preserved` | ✅ Automated |
| `replace_normalized_sleep` inserts row | §7.1 | `test_replace_normalized_sleep_inserts_row` | ✅ Automated |
| `replace_normalized_sleep` deletes existing row first | §7.1 | `test_replace_normalized_sleep_deletes_existing` | ✅ Automated |
| `delete_normalized_sleep` removes row | §8.1 | `test_delete_normalized_sleep_removes_row` | ✅ Automated |
| Sleep ops scoped by user and connection | §6.2 | `test_sleep_ops_scoped_by_user_and_connection`, `test_sleep_ops_wrong_user_not_deleted` | ✅ Automated |
| No raw logging in replace paths (privacy) | §10 | `test_no_raw_logging_in_replace_paths` | ✅ Automated |

### 2.7 Sync Integration (Sleep & Measurement Fixture Scenarios)

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Sleep sync produces normalized rows | §1 | `test_sleep_sync_produces_normalized_sleep_rows` | ✅ Automated |
| Sleep revision replaces normalized row | §7.3 | `test_sleep_revision_replaces_normalized_sleep_row` | ✅ Automated |
| Incomplete→complete sleep revision | §11.4 | `test_t6_incomplete_to_complete_sleep_revision` | ✅ Automated |
| Cross-midnight sleep local-date | §11.5 | `test_t6_cross_midnight_sleep_local_date` | ✅ Automated |
| Nap (<2h) treated identically | §9.4 | `test_t6_nap_sleep_short_duration` | ✅ Automated |
| Split same local-date sessions | §9.4 | `test_t6_split_same_local_date_two_sessions` | ✅ Automated |
| DST spring/fall sleep | §9.3 | `test_t6_dst_spring_forward_sleep`, `test_t6_dst_fall_back_sleep` | ✅ Automated |
| Overlapping sessions | §9.4 | `test_t6_overlapping_sleep_sessions` | ✅ Automated |
| Sleep tombstone deletes normalized row | §8.2 | `test_t6_sleep_tombstone_deletes_normalized_row` | ✅ Automated |
| Missing optional fields (sleep) | §5.2 | `test_t6_sleep_missing_optional_fields` | ✅ Automated |
| Missing optional fields (measurement) | §5.1 | `test_t6_measurement_missing_optional_fields` | ✅ Automated |
| Fake provider sleep scenario selection | §1 | `test_t6_fake_provider_selects_sleep_scenarios_offline` | ✅ Automated |
| Fake provider sleep tombstones | §8.2 | `test_t6_fake_provider_sleep_tombstones_offline` | ✅ Automated |

### 2.8 Read Models — Connection Freshness

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Fresh connection (≤7d last_success_at) | §10.5 | `test_get_connection_freshness_fresh` | ✅ Automated |
| Stale connection (>7d) | §10.5 | `test_get_connection_freshness_stale` | ✅ Automated |
| No sync ever → stale | §10.5 | `test_get_connection_freshness_no_sync` | ✅ Automated |
| Wrong user → returns `None` | §10.5 | `test_get_connection_freshness_wrong_user_id_returns_none` | ✅ Automated |

### 2.9 Read Models — Weight Queries

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Latest weight with data | §10.3 | `test_get_weight_latest_with_data` | ✅ Automated |
| No weight data → None/empty | §10.3 | `test_get_weight_no_data` | ✅ Automated |
| 7-day trend computed at query time | §10.3 | `test_get_weight_7d_trend` | ✅ Automated |
| 30-day trend computed at query time | §10.3 | `test_get_weight_30d_trend` | ✅ Automated |
| Strict user filtering (`WHERE user_id = $1`) | §10.1 | `test_get_weight_strict_user_filtering` | ✅ Automated |
| Tombstone-safe reads | §10.6 | `test_weight_reads_are_tombstone_safe` | ✅ Automated |
| Null handling for missing optional fields | §5.1 | `test_get_weight_null_handling` | ✅ Automated |
| No-data determinism (consistent empty shape) | §10.3 | `test_get_weight_no_data_deterministic` | ✅ Automated |

### 2.10 Read Models — Sleep Queries

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Nightly sleep single session | §10.4 | `test_get_nightly_sleep_single_session` | ✅ Automated |
| Nightly sleep multiple sessions (grouped by local_sleep_date) | §10.4 | `test_get_nightly_sleep_multiple_sessions` | ✅ Automated |
| No sleep sessions → empty | §10.4 | `test_get_nightly_sleep_no_sessions` | ✅ Automated |
| Different date not returned | §10.4 | `test_get_nightly_sleep_different_date_not_returned` | ✅ Automated |
| Strict user filtering | §10.4 | `test_get_nightly_sleep_strict_user_filtering` | ✅ Automated |
| Rolling 7d basic | §10.4 | `test_get_sleep_rolling_7d_basic` | ✅ Automated |
| Rolling 7d no data | §10.4 | `test_get_sleep_rolling_7d_no_data` | ✅ Automated |
| Rolling 7d strict user filtering | §10.4 | `test_get_sleep_rolling_7d_strict_user_filtering` | ✅ Automated |
| Tombstone-safe reads | §10.6 | `test_sleep_reads_are_tombstone_safe` | ✅ Automated |
| Null handling for missing optional fields | §5.2 | `test_get_nightly_sleep_null_handling` | ✅ Automated |
| No-data determinism | §10.4 | `test_get_nightly_sleep_no_data_deterministic` | ✅ Automated |

### 2.11 Privacy — Tool-Level Access Control

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| `_check_health_read_scope` gates: bot_id=`hector`/`habits`, topic=fitness/habits, non-null topic_id, non-null user.id | §12.1 | (covered in `test_hector_tools.py`) | ✅ Automated |
| Coach/Mediator/Tante Rosi/Superpom cannot read health data | §12.1 | (covered in `test_hector_tools.py`) | ✅ Automated |
| Hector blocked outside fitness/habits topics | §12.1 | (covered in `test_hector_tools.py`) | ✅ Automated |
| Anonymous contexts rejected | §12.1 | (covered in `test_hector_tools.py`) | ✅ Automated |

---

## 3. Workout Projection Contract

**Source:** `docs/health/workout-projection-contract.md` (status: settled post-implementation)

### 3.1 Workout Normalization

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| All 53 Workout categories map to correct Hector label | §2.2 | `TestResolveWorkoutType.test_known_category_maps_to_label` (parametrized: all 52+ values) | ✅ Automated |
| `None` category → `"unknown"` | §2.2 | `TestResolveWorkoutType.test_none_category_resolves_to_unknown` | ✅ Automated |
| Category 999 (OTHER) → `"unknown"` | §2.2 | `TestResolveWorkoutType.test_other_category_999_resolves_to_unknown` | ✅ Automated |
| Unmapped integers → `"unknown"` | §2.2 | `TestResolveWorkoutType.test_unmapped_category_resolves_to_unknown` | ✅ Automated |
| Every taxonomy label is a non-empty string | §2.2 | `TestResolveWorkoutType.test_every_taxonomy_label_is_a_valid_hector_label` | ✅ Automated |
| `HECTOR_FITNESS_TAXONOMY_LABELS` is subset of all taxonomy labels | §2.2 | Same selector above | ✅ Automated |
| Running workout with full metrics decoded correctly | §3 | `TestNormalizeWorkoutCommonCategories.test_running_workout_with_full_metrics` | ✅ Automated |
| Walking, cycling, swimming, strength, yoga, hiking, skiing, triathlon | §3 | (8 individual tests per category) | ✅ Automated |
| Unknown category from missing metadata | §3 | `TestNormalizeWorkoutUnknownCategory.test_unknown_category_from_none_source_metadata` | ✅ Automated |
| Unknown category 999 | §3 | `TestNormalizeWorkoutUnknownCategory.test_unknown_category_999_other` | ✅ Automated |
| Unknown category still decodes other metrics | §3 | `TestNormalizeWorkoutUnknownCategory.test_unknown_category_does_not_block_other_metrics` | ✅ Automated |
| No data field → all optionals `None` | §3.2 | `TestNormalizeWorkoutMissingOptional.test_no_data_field_at_all` | ✅ Automated |
| Empty data dict → all optionals `None` | §3.2 | `TestNormalizeWorkoutMissingOptional.test_empty_data_dict` | ✅ Automated |
| Partial data → missing fields `None` | §3.2 | `TestNormalizeWorkoutMissingOptional.test_partial_data_fields` | ✅ Automated |
| Missing device ID/model → `None` | §3.2 | `TestNormalizeWorkoutMissingOptional.test_missing_device_id_and_model` | ✅ Automated |
| Missing timezone → UTC date fallback | §4.3 | `TestNormalizeWorkoutMissingOptional.test_missing_timezone_falls_back_to_utc_date` | ✅ Automated |
| Missing starts_at → `None` | §3.1 | `TestNormalizeWorkoutMissingOptional.test_missing_starts_at_returns_none` | ✅ Automated |
| Duration computed from start-end delta when not in data | §3.2 | `TestNormalizeWorkoutMissingOptional.test_duration_computed_from_start_end_when_not_in_data` | ✅ Automated |
| Local date from Eastern timezone | §4.1 | `TestNormalizeWorkoutTimezoneDST.test_local_date_from_eastern_timezone` | ✅ Automated |
| Local date during standard time (EST) | §4.1 | `TestNormalizeWorkoutTimezoneDST.test_local_date_during_standard_time` | ✅ Automated |
| DST spring forward / fall back local date | §4.2 | (4 DST tests: spring forward, after transition, fall back, etc.) | ✅ Automated |
| European timezone handling | §4.1 | `TestNormalizeWorkoutTimezoneDST.test_western_european_timezone`, `test_western_european_winter_time` | ✅ Automated |
| Invalid/empty timezone → UTC fallback | §4.3 | `TestNormalizeWorkoutTimezoneDST.test_invalid_timezone_fallback_to_utc_date`, `test_empty_timezone_fallback_to_utc_date` | ✅ Automated |
| Revision count in attribution | §3.3 | `TestNormalizeWorkoutAttribution.test_revision_count_stored_in_attribution`, `test_default_revision_count_is_one` | ✅ Automated |
| Provider category in attribution | §3.3 | `TestNormalizeWorkoutAttribution.test_provider_category_in_attribution` | ✅ Automated |
| Base attribution preserved | §3.3 | `TestNormalizeWorkoutAttribution.test_base_attribution_preserved` | ✅ Automated |
| Deleted workout → `None` | §8.1 | `TestNormalizeWorkoutErrorHandling.test_deleted_workout_returns_none` | ✅ Automated |
| Non-workout / sleep record → `None` | §1 | `TestNormalizeWorkoutErrorHandling.test_non_workout_record_returns_none`, `test_sleep_record_returns_none` | ✅ Automated |
| NormalizedWorkout is frozen dataclass | §3 | `TestNormalizeWorkoutErrorHandling.test_normalized_workout_is_frozen_dataclass` | ✅ Automated |
| `ended_at` earlier than `started_at` raises | §3 | `TestNormalizeWorkoutErrorHandling.test_ended_at_earlier_than_started_at_raises` | ✅ Automated |

### 3.2 Pure Matcher — `project_workout()`

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| No local_date → `no_local_date` | §5.2 | `test_workout_without_local_date_returns_no_local_date_reason` | ✅ Automated |
| Unknown workout type rejected before commitment filter | §5.2 | `test_unknown_workout_type_rejected`, `test_unmapped_workout_type_rejected` | ✅ Automated |
| Unknown type checked before commitment filtering | §5.2 | `test_unknown_type_checked_before_commitment_filtering` | ✅ Automated |
| Empty commitment list → `zero_active_commitments` | §5.2 | `test_empty_commitment_list` | ✅ Automated |
| Non-Hector bot → filtered out | §5.2 | `test_non_hector_bot_rejected` | ✅ Automated |
| Non-fitness topic → filtered out | §5.2 | `test_non_fitness_topic_rejected` | ✅ Automated |
| Both wrong bot and topic → filtered out | §5.2 | `test_both_wrong_bot_and_topic` | ✅ Automated |
| Mixed valid/invalid → only Hector fitness considered | §5.2 | `test_mixed_valid_and_invalid_commitments_filters_correctly` | ✅ Automated |
| Date before commitment start → `no_eligible_slot` | §5.3 | `test_date_before_commitment_start` | ✅ Automated |
| Date after commitment end → `no_eligible_slot` | §5.3 | `test_date_after_commitment_end` | ✅ Automated |
| Weekday cadence on weekend → no eligible slot | §5.3 | `test_weekday_cadence_on_weekend` | ✅ Automated |
| Custom days wrong day | §5.3 | `test_custom_days_wrong_day` | ✅ Automated |
| Two commitments both eligible → `ambiguous_multiple_commitments` | §5.2 | `test_two_commitments_both_eligible`, `test_three_commitments_all_eligible` | ✅ Automated |
| Single daily commitment → `matched` | §5.2 | `test_single_daily_commitment_matches` | ✅ Automated |
| Weekday cadence on weekday → matched | §5.3 | `test_weekday_cadence_on_weekday` | ✅ Automated |
| Custom days matching day → matched | §5.3 | `test_custom_days_matching_day` | ✅ Automated |
| Weekly count any day in week → matched | §5.3 | `test_weekly_count_any_day_in_week` | ✅ Automated |
| Custom cadence within range → matched | §5.3 | `test_custom_cadence_within_range` | ✅ Automated |
| All taxonomy types accepted (24 labels) | §2.2 | `test_all_taxonomy_types_are_accepted` | ✅ Automated |
| End_date None = unbounded | §5.3 | `test_end_date_none_means_unbounded` | ✅ Automated |
| Matched → `is_projecting=True` | §5.4 | `test_matched_is_projecting` | ✅ Automated |
| All other reasons → `is_projecting=False` | §5.4 | `test_all_other_reasons_are_not_projecting` | ✅ Automated |
| Start_date None for daily matches any date | §5.3 | `test_start_date_none_daily_matches_any_date` | ✅ Automated |
| Start_date None for custom matches any date | §5.3 | `test_start_date_none_custom_matches_any_date` | ✅ Automated |
| Weekly count with future start/past end rejected | §5.3 | `test_weekly_count_with_future_start_date_rejected`, `test_weekly_count_with_past_end_date_rejected` | ✅ Automated |
| DST-aware date matching | §4.2 | `test_spring_forward_date_matches_daily_commitment`, `test_fall_back_date_matches_daily_commitment`, `test_spring_forward_date_with_weekday_cadence_on_sunday_rejected`, `test_spring_forward_monday_matches_weekday_cadence`, `test_dst_date_with_custom_days_matches`, `test_user_timezone_during_dst_preserves_local_date`, `test_dst_date_ambiguous_multiple_still_rejected` | ✅ Automated |
| User_id on commitment is informational, not a filter (caller pre-filters) | §5.2 | `test_commitment_with_different_user_id_still_matches`, `test_all_commitments_wrong_user_no_user_filter_applied` | ✅ Automated |
| Attribution ignored for matching | §3.3 | `test_attribution_field_is_ignored_for_matching` | ✅ Automated |

### 3.3 Applicator — `apply_workout_projection()`

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Disabled returns `None` immediately | §9.2 | `test_disabled_returns_none` | ✅ Automated |
| Disabled: no events in pool | §9.2 | `test_disabled_no_events_in_pool` | ✅ Automated |
| Disabled: no projection rows in pool | §9.2 | `test_disabled_no_projection_rows_in_pool` | ✅ Automated |
| First-time match: exactly one event in pool | §6 | `test_exactly_one_event_in_pool` | ✅ Automated |
| First-time match: exactly one ledger row | §6 | `test_exactly_one_ledger_row_in_pool` | ✅ Automated |
| Event linked correctly in ledger row | §6 | `test_event_linked_correctly_in_ledger_row` | ✅ Automated |
| Same source + same version → returns existing (no duplicate) | §6.1 | `test_same_source_same_version_returns_existing`, `test_idempotent_replay_no_duplicate_event`, `test_idempotent_replay_no_duplicate_ledger` | ✅ Automated |
| Different sources → independent projections | §6 | `test_different_sources_each_get_own_projection` | ✅ Automated |
| Same source, different users → independent | §6 | `test_same_source_different_users_independent` | ✅ Automated |
| Revision supersedes old in pool | §7.1 | `test_revision_supersedes_old_in_pool` | ✅ Automated |
| Revision deletes old event from pool | §7.1 | `test_revision_old_event_removed_from_pool` | ✅ Automated |
| Revision chain visible in pool | §7.1 | `test_revision_chain_visible_in_pool` | ✅ Automated |
| Rematch different commitment supersedes old | §7.2 | `test_rematch_different_commitment_supersedes_old` | ✅ Automated |
| Rematch no eligible → cleanup (no new event) | §7.2 | `test_rematch_no_eligible_commitment_cleanup_in_pool` | ✅ Automated |
| Tombstone removes projection in pool | §7.3 | `test_tombstone_removes_projection_in_pool` | ✅ Automated |
| Tombstone deletes projection event from pool | §7.3 | `test_tombstone_deletes_projection_event_from_pool` | ✅ Automated |
| Tombstone no existing projection → noop | §7.3 | `test_tombstone_no_existing_projection_is_noop` | ✅ Automated |
| Tombstone disabled → noop | §7.3 | `test_tombstone_disabled_is_noop` | ✅ Automated |
| Manual event survives tombstone | §7.4 | `test_manual_event_survives_tombstone` | ✅ Automated |
| Manual event survives revision | §7.4 | `test_manual_event_survives_revision` | ✅ Automated |
| `find_projection_by_event` isolates manual events | §7.4 | `test_find_projection_by_event_isolates_manual` | ✅ Automated |

### 3.4 Repository / FakePool Projection Primitives

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| `insert_projection` populates all fields | §6 | `test_insert_projection_populates_all_fields` | ✅ Automated |
| `find_active_projection` returns only active | §7.5 | `test_find_active_projection_returns_only_active` | ✅ Automated |
| `find_active_projection` returns None for different user | §6 | `test_find_active_projection_returns_none_for_different_user` | ✅ Automated |
| `find_active_projection(for_update=True)` locks | §6.2 | `test_find_active_projection_for_update_locks` | ✅ Automated |
| `find_projection_by_event` returns owner | §7.4 | `test_find_projection_by_event_returns_owner` | ✅ Automated |
| `find_projection_by_event` returns None for manual | §7.4 | `test_find_projection_by_event_returns_none_for_manual` | ✅ Automated |
| `supersede_projection` changes status + detaches | §7.1 | `test_supersede_projection_changes_status` | ✅ Automated |
| `supersede_projection` wrong user raises | §7.1 | `test_supersede_projection_wrong_user_raises` | ✅ Automated |
| `remove_projection` changes status + detaches event | §7.3 | `test_remove_projection_changes_status_and_detaches_event` | ✅ Automated |
| `remove_projection` wrong user raises | §7.3 | `test_remove_projection_wrong_user_raises` | ✅ Automated |
| `detach_projection_event` sets event_id to null | §7.1 | `test_detach_projection_event_sets_null` | ✅ Automated |
| `detach_projection_event` wrong user raises | §7.1 | `test_detach_projection_event_wrong_user_raises` | ✅ Automated |
| Multiple versions: only one active | §6.3 | `test_multiple_versions_only_one_active` | ✅ Automated |
| `create_projection_event` populates all fields | §6 | `test_create_projection_event_populates_all_fields` | ✅ Automated |
| `delete_projection_event` removes from pool | §7.3 | `test_delete_projection_event_removes_from_pool` | ✅ Automated |
| `delete_projection_event` wrong user fails | §7.3 | `test_delete_projection_event_wrong_user_fails` | ✅ Automated |
| FakePool copy includes projections | §6 | `test_fake_pool_copy_includes_projections` | ✅ Automated |
| FakePool replace restores empty projections | §6 | `test_fake_pool_replace_restores_empty_projections` | ✅ Automated |
| `delete_projection_event` fails on wrong user (applicator-level) | §7.4 | `test_delete_projection_event_fails_on_wrong_user` | ✅ Automated |

### 3.5 Read Models — Workout Queries

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| `get_recent_workouts` no workouts → empty | §10.3 | `test_no_workouts_returns_empty` (in TestRecentWorkouts) | ✅ Automated |
| Limit respected | §10.3 | `test_no_workouts_limit_respected` | ✅ Automated |
| Single workout, no projection | §10.3 | `test_single_workout_no_projection` | ✅ Automated |
| Multiple workouts ordered desc | §10.3 | `test_multiple_workouts_ordered_desc` | ✅ Automated |
| Limit truncates | §10.3 | `test_limit_truncates_results` | ✅ Automated |
| Workout with all optional metrics | §10.3 | `test_workout_with_all_optional_metrics` | ✅ Automated |
| Projected state resolution | §10.2 | `test_projected_state` | ✅ Automated |
| Unmatched state (no eligible slot) | §10.2 | `test_unmatched_state_no_eligible_slot` | ✅ Automated |
| Ambiguous state | §10.2 | `test_ambiguous_state` | ✅ Automated |
| Removed state | §10.2 | `test_removed_state` | ✅ Automated |
| Duplicate-linked state | §10.2 | `test_duplicate_linked_state` | ✅ Automated |
| None state (no projection row) | §10.2 | `test_none_state_no_projection_row` | ✅ Automated |
| Mixed projection states | §10.2 | `test_mixed_projection_states` | ✅ Automated |
| Only current user's workouts returned | §11.1 | `test_only_current_user_workouts_returned` | ✅ Automated |
| Projections not leaked across users | §11.1 | `test_projections_not_leaked_across_users` | ✅ Automated |
| `get_weekly_workout_summary` no workouts → empty | §10.4 | `test_no_workouts_returns_empty` (in TestWeeklyWorkoutSummary) | ✅ Automated |
| No workouts in window | §10.4 | `test_no_workouts_in_window` | ✅ Automated |
| Single workout single day | §10.4 | `test_single_workout_single_day` | ✅ Automated |
| Multiple workouts same day | §10.4 | `test_multiple_workouts_same_day` | ✅ Automated |
| Workouts across multiple days | §10.4 | `test_workouts_across_multiple_days` | ✅ Automated |
| Projected count aggregation | §10.4 | `test_projected_count_aggregation` | ✅ Automated |
| Cross-user isolation (weekly) | §11.1 | `test_weekly_summary_cross_user_isolation`, `test_weekly_summary_projected_count_not_leaked_across_users` | ✅ Automated |
| Local date from offset (east/west) | §10.2 | `test_local_date_from_offset_east`, `test_local_date_from_offset_west` | ✅ Automated |
| Local date fallback (no offset) | §10.2 | `test_local_date_fallback_no_offset` | ✅ Automated |
| Weekly summary only returns requesting user | §11.1 | `test_weekly_summary_only_returns_requesting_user_workouts` | ✅ Automated |
| No projection leak from other user | §11.1 | `test_recent_workouts_no_projection_leak_from_other_user` | ✅ Automated |
| Empty for user with no health connection | §11.1 | `test_weekly_summary_empty_for_user_with_no_health_connection`, `test_recent_workouts_empty_for_user_with_no_connection` | ✅ Automated |

### 3.6 Privacy

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Tool-level gates (bot, topic, user) match weight/sleep | §11.2 | (covered in `test_hector_tools.py`) | ✅ Automated |
| Hot context omits raw payloads, tokens, HR detail, device IDs | §11.3 | (covered in `test_hot_context_hector.py`, `test_hector_hot_context.py`) | ✅ Automated |
| Projection ledger separate from manual events | §11.4 | `test_manual_event_survives_tombstone`, `test_manual_event_survives_revision`, `test_find_projection_by_event_isolates_manual` | ✅ Automated |

### 3.7 Type-Safety Invariant

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Only events with `adherence_status` + matching `commitment_id` can classify slots | §8.1 | (covered in `test_adherence.py`) | ✅ Automated |
| Weight/sleep/numeric events cannot satisfy workout commitment | §8.1 | (covered in `test_adherence.py`) | ✅ Automated |
| `commitments.py` is compatibility shim delegating to `adherence.py` | §8.3 | (covered in `test_commitments.py`) | ✅ Automated |

---

## 4. Observability — Metrics & Alerts

**Source:** `app/services/health_sync/metrics.py` (task T7), using the existing log-based `app/services/metrics.py` layer.

All health-sync observability uses sanitized labels only: **provider**, **resource_type**, **status**, **error_kind**, and **retryable**. No user ids, provider user ids, tokens, raw payloads, device ids, or health values appear in any metric label or value.

### 4.1 Metric Names & Labels

| Metric Name | Labels | Kind | What It Measures | Test Selector |
|---|---|---|---|---|
| `health_sync_attempts_started` | `provider`, `resource_type` | Counter | Sync attempt started (before retries) | `test_all_expected_helpers_exist` |
| `health_sync_attempts_completed` | `provider`, `resource_type`, `status`, `error_kind`, `retryable` | Counter | Final sync result (success/partial/failed) | `test_all_expected_helpers_exist` |
| `health_sync_duration_seconds` | `provider`, `resource_type`, `status` | Histogram | Wall-clock duration of sync | `test_all_expected_helpers_exist` |
| `health_sync_records_fetched` | `provider`, `resource_type` | Counter | Records (incl. tombstones) fetched from provider | `test_all_expected_helpers_exist` |
| `health_sync_records_deleted` | `provider`, `resource_type` | Counter | Tombstone records received | `test_all_expected_helpers_exist` |
| `health_sync_retry` | `provider`, `resource_type`, `retryable` | Counter | Retry event attempted or skipped | `test_all_expected_helpers_exist` |
| `health_sync_cursor_errors` | `provider`, `resource_type`, `error_kind` | Counter | Cursor-state errors that abort a sync | `test_all_expected_helpers_exist` |
| `health_sync_stale_freshness` | `provider`, `resource_type` | Counter | Connection classified as stale (>24h) | `test_all_expected_helpers_exist` |
| `health_sync_projection_outcome` | `provider`, `resource_type`=`workout`, `status`, `error_kind`, `retryable` | Counter | Projection decision (projected/no_match/removed/error) | `test_all_expected_helpers_exist` |
| `health_sync_worker_claimed` | `provider` | Gauge | Dirty categories claimed in a scan | `test_all_expected_helpers_exist` |
| `health_sync_worker_synced` | `provider` | Gauge | Dirty categories successfully synced | `test_all_expected_helpers_exist` |
| `health_sync_worker_failed` | `provider` | Gauge | Dirty categories that failed | `test_all_expected_helpers_exist` |
| `health_sync_worker_skipped_disabled` | `provider` | Gauge | Dirty categories skipped (category disabled) | `test_all_expected_helpers_exist` |
| `health_sync_worker_reconciliation_outcomes` | `provider` | Gauge | Reconciliation outcomes processed | `test_all_expected_helpers_exist` |
| `health_sync_worker_skipped_connections` | `provider` | Gauge | Connections skipped in scan | `test_all_expected_helpers_exist` |
| `health_sync_worker_scanned_connections` | `provider` | Gauge | Total connections scanned | `test_all_expected_helpers_exist` |

### 4.2 Alert Names & Guidance

These are operator-facing alert thresholds derived from metric names. All alerts are **advisory** pending live validation (gap L-009).

| Alert Name | Based On | Threshold / Condition | Severity |
|---|---|---|---|
| `HealthSyncPermanentFailure` | `health_sync_attempts_completed{status="permanent_failure"}` | Non-zero on enabled categories | **High** — may indicate reauthorization required or API entitlement issue |
| `HealthSyncStaleFreshness` | `health_sync_stale_freshness` | Non-zero on enabled categories | **Medium** — >24h without successful sync for an enabled category |
| `HealthSyncHighRetryRate` | `health_sync_retry{retryable="true"}` | Sustained elevated rate | **Low** — may indicate rate-limiting; adjust `HEALTH_SYNC_RETRY_AFTER_CAP_SECONDS` |
| `HealthSyncCursorError` | `health_sync_cursor_errors` | Non-zero on any category | **Medium** — cursor state corruption; may require manual intervention |
| `HealthSyncWorkerZeroScans` | `health_sync_worker_scanned_connections` | Zero across multiple scan cycles with connections present | **High** — worker may be stuck or disabled |

### 4.3 Metric Privacy Boundary (Automated Evidence)

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| All 16 metric helpers exist in `app/services/health_sync/metrics.py` | — | `test_all_expected_helpers_exist` | ✅ Automated |
| All metric function parameters use only safe labels (no tokens, user_ids, etc.) | — | `test_metric_function_parameters_are_safe` | ✅ Automated |
| All `_incr`/`_gauge`/`_observe` calls use only safe keyword labels | — | `test_metric_incr_calls_use_safe_labels_only` | ✅ Automated |
| Module docstring states the privacy promise explicitly | — | `test_metric_module_docstring_privacy_promise` | ✅ Automated |

---

## 5. Privacy Guardrail Evidence

**Source:** `tests/test_health_privacy_surfaces.py` (task T13, 42 tests across 8 classes).

The privacy surfaces regression suite scans every default surface that could leak tokens, secrets, health values, device identifiers, or raw payloads. Health values are permitted **only** in the explicit authenticated export endpoint and explicit health read tools.

### 5.1 Route Response Surfaces

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| `/api/health/devices/withings/status` excludes encrypted tokens | `test_status_excludes_encrypted_tokens` | ✅ Automated |
| `/api/health/devices/withings/status` excludes `external_user_id` | `test_status_excludes_external_user_id` | ✅ Automated |
| `/api/health/devices/withings/status` excludes device IDs | `test_status_excludes_device_ids` | ✅ Automated |
| `/api/health/devices/withings/status` excludes health values | `test_status_excludes_health_values` | ✅ Automated |
| `/api/health/devices/withings/status` excludes OAuth state | `test_status_excludes_oauth_state` | ✅ Automated |
| `/api/health/devices/withings/resync` excludes tokens and device IDs | `test_resync_excludes_tokens_and_device_ids` | ✅ Automated |
| `/api/health/devices/withings/resync` excludes health values | `test_resync_excludes_health_values` | ✅ Automated |
| `/api/health/devices/withings/disconnect` excludes tokens and provider IDs | `test_disconnect_excludes_tokens_and_provider_ids` | ✅ Automated |
| `/api/health/devices/withings/disconnect` excludes health values | `test_disconnect_excludes_health_values` | ✅ Automated |
| `DELETE /api/health/devices/withings` excludes tokens and provider IDs | `test_delete_excludes_tokens_and_provider_ids` | ✅ Automated |
| `DELETE /api/health/devices/withings` excludes health values | `test_delete_excludes_health_values` | ✅ Automated |
| `GET /api/health/devices/withings/connect` excludes tokens and health values | `test_connect_excludes_tokens_and_health_values` | ✅ Automated |

### 5.2 Admin Diagnostics Surfaces

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| `/admin/health` excludes encrypted tokens | `test_admin_health_excludes_encrypted_tokens` | ✅ Automated |
| `/admin/health` excludes `cursor_state` | `test_admin_health_excludes_cursor_state` | ✅ Automated |
| `/admin/health` excludes `external_user_id` | `test_admin_health_excludes_external_user_id` | ✅ Automated |
| `/admin/health` excludes health values | `test_admin_health_excludes_health_values` | ✅ Automated |
| `/admin/health` excludes device IDs | `test_admin_health_excludes_device_ids` | ✅ Automated |
| `/admin/health` excludes OAuth timestamps | `test_admin_health_excludes_oauth_timestamps` | ✅ Automated |
| `/admin/health` only metadata columns present | `test_admin_health_metadata_only_columns_present` | ✅ Automated |

### 5.3 Tool Registry & Prompt Surfaces

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| Health tool descriptions exist and are prompt-safe | `test_tool_descriptions_are_prompt_safe` | ✅ Automated |
| Tool descriptions contain no raw health values | `test_health_tool_descriptions_no_raw_health_values` | ✅ Automated |
| Tool descriptions contain no device IDs | `test_health_tool_descriptions_no_device_ids` | ✅ Automated |
| Hector fitness block has health summaries (no raw payloads) | `test_hector_fitness_block_no_raw_payloads` | ✅ Automated |
| Hector fitness block has no device IDs | `test_hector_fitness_block_no_device_ids` | ✅ Automated |
| Hector fitness block has no tokens | `test_hector_fitness_block_no_tokens` | ✅ Automated |
| Health read guidance prompt has no secrets | `test_health_read_guidance_prompt_no_secrets` | ✅ Automated |
| Health read guidance prompt has no health values | `test_health_read_guidance_prompt_no_health_values` | ✅ Automated |
| Health read guidance prompt has no device IDs | `test_health_read_guidance_prompt_no_device_ids` | ✅ Automated |
| Health read guidance prompt references privacy boundaries | `test_health_read_guidance_prompt_references_boundaries` | ✅ Automated |

### 5.4 Health Read Tool Output Surfaces (Permitted Exception)

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| `get_weight_trend` output contains aggregate values (not raw) | `test_get_weight_trend_output_contains_aggregates` | ✅ Automated |
| `get_workout_summary` output excludes heart rate detail | `test_get_workout_summary_output_no_heart_rate_detail` | ✅ Automated |
| `get_sleep_summary` output excludes stage timelines | `test_get_sleep_summary_output_no_stage_timelines` | ✅ Automated |

### 5.5 Export Surface (Permitted Exception)

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| Export contains health values (explicit exception surface) | `test_export_contains_health_values` | ✅ Automated |
| Export contains device IDs (user's own data) | `test_export_contains_device_ids` | ✅ Automated |
| Export excludes tokens | `test_export_excludes_tokens` | ✅ Automated |
| Export contains `external_user_id` (user's own provider identifier) | `test_export_contains_external_user_id` | ✅ Automated |

---

## 6. Admin Diagnostics Surface

**Source:** `app/routers/admin.py` (`/admin/health`), task T8. Test coverage: `tests/test_admin_health.py` (21 tests).

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| Admin health page requires HTTP Basic Auth | (covered in `test_admin_health.py`) | ✅ Automated |
| Page renders config flags (enabled/disabled) for all health env vars | (covered in `test_admin_health.py`) | ✅ Automated |
| Page lists connections: id, user_id, provider, status, timestamps, stale class | (covered in `test_admin_health.py`) | ✅ Automated |
| Page shows summary counts: total, active, stale, never-synced | (covered in `test_admin_health.py`) | ✅ Automated |
| Page shows best-effort sync run and projection totals | (covered in `test_admin_health.py`) | ✅ Automated |
| Page excludes tokens (access, refresh, encrypted) | `test_admin_health_excludes_encrypted_tokens` | ✅ Automated |
| Page excludes `external_user_id` | `test_admin_health_excludes_external_user_id` | ✅ Automated |
| Page excludes `cursor_state` | `test_admin_health_excludes_cursor_state` | ✅ Automated |
| Page excludes health values | `test_admin_health_excludes_health_values` | ✅ Automated |
| Page excludes device IDs | `test_admin_health_excludes_device_ids` | ✅ Automated |
| Page excludes OAuth timestamps | `test_admin_health_excludes_oauth_timestamps` | ✅ Automated |
| Follows existing admin conventions (HTML/Pico CSS, `_page`/`_table` helpers) | (covered in `test_admin_health.py`) | ✅ Automated |
| Graceful `FakePool` fallback for sync/projection totals | (covered in `test_admin_health.py`) | ✅ Automated |

---

## 7. Lifecycle & Data Portability

### 7.1 Export & Data Portability

**Source:** `app/services/health_sync/export.py` + `app/routers/health_devices.py` (`GET /api/health/devices/withings/export`), task T4. Test coverage: `tests/test_health_export.py` (14+ tests).

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Authenticated access control (401 unauthenticated) | — | (covered in `test_health_export.py`) | ✅ Automated |
| Connection metadata returned (no tokens, no `cursor_state`) | — | (covered in `test_health_export.py`) | ✅ Automated |
| Source-record provenance metadata returned (no raw payloads) | — | (covered in `test_health_export.py`) | ✅ Automated |
| Normalized measurement rows returned with health values | — | (covered in `test_health_export.py`) | ✅ Automated |
| Normalized sleep rows returned | — | (covered in `test_health_export.py`) | ✅ Automated |
| Normalized workout rows returned | — | (covered in `test_health_export.py`) | ✅ Automated |
| Projection ledger rows returned | — | (covered in `test_health_export.py`) | ✅ Automated |
| Dirty categories returned | — | (covered in `test_health_export.py`) | ✅ Automated |
| Encrypted tokens excluded | — | `test_export_excludes_tokens` | ✅ Automated |
| External user ID included (user's own data) | — | `test_export_contains_external_user_id` | ✅ Automated |
| Cross-user isolation (only current user's rows) | — | (covered in `test_health_export.py`) | ✅ Automated |
| Health values present (this is the explicit exception surface) | — | `test_export_contains_health_values` | ✅ Automated |
| `cursor_state` excluded | — | (covered in `test_health_export.py`) | ✅ Automated |
| OAuth state / webhook form payloads excluded | — | (covered in `test_health_export.py`) | ✅ Automated |
| Raw provider payloads excluded | — | (covered in `test_health_export.py`) | ✅ Automated |

### 7.2 Delete & Local Cleanup

**Source:** `app/services/health_sync/repository.py` + `app/routers/health_devices.py` (`DELETE /api/health/devices/withings`), tasks T5/T6. Test coverage: `tests/test_health_export_delete.py` (11 tests), `tests/test_health_delete.py` (multiple tests), `tests/test_health_projection_applicator.py` (7 tests).

| Invariant | Contract § | Test Selector(s) | Evidence Status |
|---|---|---|---|
| Auth required (401 unauthenticated) | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| 503 when `HEALTH_SYNC_ENABLED=false` | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| 404 when no connection exists | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Metadata-only response (no health values) | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Connection marked deleted, tokens cleared | §4 | (covered in `test_health_export_delete.py` / `test_health_delete.py`) | ✅ Automated |
| Source records deleted | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Normalized measurements/sleep/workouts deleted | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Dirty categories deleted | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Webhook receipts deleted | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Projection ledger rows deleted | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Projection-owned adherence events deleted (ledger-subquery-scoped) | — | (covered in `test_health_export_delete.py` / `test_health_projection_applicator.py`) | ✅ Automated |
| Manual adherence events survive | — | `test_manual_event_survives_tombstone` | ✅ Automated |
| Cross-user deletion rejected | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Other user's data preserved | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Idempotent (repeated delete safe) | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| Best-effort revoke before local teardown | — | (covered in `test_health_export_delete.py`) | ✅ Automated |
| All operations double-scoped (`connection_id` + `user_id`) | — | (covered in `test_health_delete.py`) | ✅ Automated |
| Transactionally safe (all removals in one transaction) | — | (covered in `test_health_delete.py`) | ✅ Automated |

---

## 8. Synthetic Canary Evidence

**Source:** `tests/test_health_synthetic_canary.py` (tasks T11/T12, 18 tests across 3 classes). All canaries use `FakeWithingsProvider` and `FakePool` — no live network calls, tokens, or provider secrets.

### 8.1 Weight Synthetic Canary (5 tests, `TestWeightSyntheticCanary`)

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| Fake OAuth → reconciliation → sync produces a weigh-in in `health_normalized_measurements` | (covered in `TestWeightSyntheticCanary`) | ✅ Automated |
| `get_weight()` read model returns correct latest weight and trends | (covered in `TestWeightSyntheticCanary`) | ✅ Automated |
| `get_weight_trend` health read tool returns valid output via `TurnContext` | (covered in `TestWeightSyntheticCanary`) | ✅ Automated |
| Empty/no-data user returns empty results | (covered in `TestWeightSyntheticCanary`) | ✅ Automated |
| User scoping: other user's weight is not returned | (covered in `TestWeightSyntheticCanary`) | ✅ Automated |

### 8.2 Sleep Synthetic Canary (6 tests, `TestSleepSyntheticCanary`)

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| Late sleep revision (incomplete → completed) updates rolling summary without stale duplicates | (covered in `TestSleepSyntheticCanary`) | ✅ Automated |
| `get_sleep_rolling_7d()` returns updated data after revision | (covered in `TestSleepSyntheticCanary`) | ✅ Automated |
| Tombstone deletes normalized sleep rows | (covered in `TestSleepSyntheticCanary`) | ✅ Automated |
| Cross-midnight sleep sessions handled correctly | (covered in `TestSleepSyntheticCanary`) | ✅ Automated |
| User scoping: other user's sleep is not returned | (covered in `TestSleepSyntheticCanary`) | ✅ Automated |
| Empty/no-data user returns empty results | (covered in `TestSleepSyntheticCanary`) | ✅ Automated |

### 8.3 Workout Synthetic Canary (7 tests, `TestWorkoutSyntheticCanary`)

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| Workout sync produces normalized rows in `health_normalized_workouts` | (covered in `TestWorkoutSyntheticCanary`) | ✅ Automated |
| Exactly-once projection: one compatible Hector fitness commitment → one event + one ledger row | (covered in `TestWorkoutSyntheticCanary`) | ✅ Automated |
| Idempotent replay returns existing projection with no duplicates | (covered in `TestWorkoutSyntheticCanary`) | ✅ Automated |
| Tombstone reverses projection (removes event, marks projection `removed`, detaches event link) | (covered in `TestWorkoutSyntheticCanary`) | ✅ Automated |
| Manual events survive tombstone unscathed | (covered in `TestWorkoutSyntheticCanary`) | ✅ Automated |
| Cross-user isolation: `find_active_projection` and tombstone are user-scoped | (covered in `TestWorkoutSyntheticCanary`) | ✅ Automated |
| Geometry of projection pool stays clean after full create → replay → tombstone cycle | (covered in `TestWorkoutSyntheticCanary`) | ✅ Automated |

---

## 9. Failure Drill Coverage

**Source:** `tests/test_health_failure_drills.py` (task T9, 27 tests across 7 classes).

| Scenario | Expected Behavior | Test Class | Evidence Status |
|---|---|---|---|
| Stale freshness classification | Never synced → stale; successful sync → fresh; old sync → stale; failed sync doesn't update freshness; user-scoped | `TestStaleCursorFreshness` (6 tests) | ✅ Automated |
| Reauthorization required | Permanent failure recorded; existing records preserved; connection marked `reauth_required` | `TestReauthorizationRequired` (3 tests) | ✅ Automated |
| Rate-limiting | Below cap: retries up to `HEALTH_SYNC_MAX_ATTEMPTS`; above cap: fails immediately | `TestRateLimitRetryAfter` (3 tests) | ✅ Automated |
| Webhook without fetch | Transient failure → retry succeeds; records synced after recovery; duplicate webhook deduplicated | `TestWebhookWithoutFetchRecovery` (3 tests) | ✅ Automated |
| Duplicate records | Duplicate source record ignored; existing normalized rows preserved; no double-counting | `TestDuplicateRecords` (3 tests) | ✅ Automated |
| Cursor crash rollback | Transaction rolled back; cursor not advanced; dirty category remains for retry | `TestCursorCrashTransactionRollback` (3 tests) | ✅ Automated |
| Projection drift | Revision supersedes; rematch to different commitment; tombstone cleanup; manual events survive all | `TestProjectionDrift` (6 tests) | ✅ Automated |

---

## 10. Weekly Digest Generator

**Source:** `app/services/health_sync/weekly_summary.py` (task T14). Test coverage: `tests/test_health_weekly_summary.py` (9 classes, multiple tests). Flag: `HEALTH_WEEKLY_SUMMARY_ENABLED=false` (default-off).

The weekly digest is a **pure read-only generator** with no side effects, no writes, no message sending, and no job scheduling. It is not yet integrated into the scheduler or prompt system.

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| Disabled flag returns empty digest | `TestWeeklyDigestDisabled` | ✅ Automated |
| No connection returns empty digest | `TestWeeklyDigestNoConnection` | ✅ Automated |
| Empty data returns sensible defaults | `TestWeeklyDigestEmptyData` | ✅ Automated |
| Weight-only data produces correct digest | `TestWeeklyDigestWeightOnly` | ✅ Automated |
| Sleep-only data produces correct digest | `TestWeeklyDigestSleepOnly` | ✅ Automated |
| Workout-only data produces correct digest | `TestWeeklyDigestWorkoutOnly` | ✅ Automated |
| Full multi-category data produces complete digest | `TestWeeklyDigestFullData` | ✅ Automated |
| User scoping: only current user's data returned | `TestWeeklyDigestUserScoping` | ✅ Automated |
| `WeeklyHealthDigest` dataclass defaults are sensible | `TestWeeklyDigestDataclassDefaults` | ✅ Automated |
| No writes, no side effects, no message sending (pure read-only) | (covered in `test_health_weekly_summary.py`) | ✅ Automated |

---

## 11. Config & Rollout Guard Evidence

**Source:** `tests/test_health_config.py` (13 tests), `tests/test_health_release_readiness.py` (33 tests).

### 11.1 Default-Off Contract

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| `HEALTH_SYNC_ENABLED` defaults to `False` | `test_health_sync_defaults_off_and_starts_without_provider_secrets` | ✅ Automated |
| `HEALTH_SYNC_MEASUREMENTS_ENABLED` defaults to `False` | Same selector above | ✅ Automated |
| `HEALTH_SYNC_WORKOUTS_ENABLED` defaults to `False` | Same selector above | ✅ Automated |
| `HEALTH_SYNC_SLEEP_ENABLED` defaults to `False` | Same selector above | ✅ Automated |
| `HEALTH_WORKOUT_PROJECTION_ENABLED` defaults to `False` | Same selector above | ✅ Automated |
| `HEALTH_WEEKLY_SUMMARY_ENABLED` defaults to `False` | Same selector above | ✅ Automated |
| `Settings()` constructs without Withings credentials or encryption key | Same selector above | ✅ Automated |

### 11.2 Proof-Map Contract Tests

| Invariant | Test Selector | Evidence Status |
|---|---|---|
| Proof-map references all three upstream handoff contracts | `TestProofMapReferencesHandoffContracts` (3 tests) | ✅ Automated |
| Proof-map references core health test selectors across all contract areas | `TestProofMapReferencesHealthSelectors` (multiple tests) | ✅ Automated |
| Proof-map does NOT claim live rollout, production enablement, vendor approval, legal review, or completed dogfood | `TestProofMapDoesNotClaimRollout` (5 assertion categories) | ✅ Automated |

---

## 4. Gap Summary

### 4.1 Automated Test Gaps

| Gap ID | Contract | Invariant | Severity | Notes |
|---|---|---|---|---|
| G-001 | Weight & Sleep | Missing `value` or `unit` fields raise `KeyError` (contract §2.2) | Low | This is defensive — the normalizer doesn't catch it explicitly in a unit test, but the model dataclass enforces required fields at construction. |
| G-002 | Provider Contract | Route `HEAD` endpoint returns exactly HTTP 200 on callback and notification routes (§2) | Low | Covered implicitly by route registration but not as a dedicated contract test. |

### 4.2 Pending Live Validation Gaps

These require **operator or vendor action** outside the CI pipeline and **cannot** be verified by automated tests:

| Gap ID | Contract | Prerequisite | Blocking Rollout? | Notes |
|---|---|---|---|---|
| L-001 | Provider §13 | `DATA_ENCRYPTION_KEY` provisioned | **Yes** — no encrypted token storage without it | Operator action |
| L-002 | Provider §13 | `WITHINGS_CLIENT_ID` / `WITHINGS_CLIENT_SECRET` provisioned | **Yes** — OAuth flow requires credentials | Operator action |
| L-003 | Provider §13 | HTTPS callback URL registered at exact endpoint | **Yes** — redirect mismatch will fail | Operator action |
| L-004 | Provider §13 | HTTPS notification endpoint exposed | **Yes** — Withings will reject plain HTTP | Operator action |
| L-005 | Provider §13 | Live Withings API entitlement & approval | **Yes** — no data without it | Vendor action |
| L-006 | Provider §13 | Notification subscribe flow verified (openAPI vs human guide conflict) | **Yes** — can't trust the spec without a demo | Live validation |
| L-007 | Provider §13 | All health flags off until credentials ready | Procedural | Operator action |
| L-008 | Workout Projection §9.1 | `health_workout_projection_enabled` must stay `False` until vendor live | Procedural | Currently default-off by code contract; no live test possible |
| L-009 | All | 24-hour staleness threshold for enabled categories | Low | Documented as default gate; sub-threshold not yet justified by test evidence |
| L-010 | All | Live Withings `revoke` endpoint integration | Low | Contract §3 notes local-only revoke until external user identifier is available |

### 4.3 Evidence Count

| Category | Count |
|---|---|
| ✅ **Automated test selectors (provider contract)** | 10+ distinct test selectors mapped |
| ✅ **Automated test selectors (weight/sleep read model)** | 80+ distinct test selectors mapped |
| ✅ **Automated test selectors (workout projection)** | 110+ distinct test selectors mapped |
| ✅ **Automated test selectors (observability metrics)** | 16 metric names + 4 privacy boundary tests mapped |
| ✅ **Automated test selectors (privacy guardrails)** | 42 tests across 8 classes mapped |
| ✅ **Automated test selectors (admin diagnostics)** | 13 invariants + 21 tests mapped |
| ✅ **Automated test selectors (export & data portability)** | 15 invariants + 14+ tests mapped |
| ✅ **Automated test selectors (delete & cleanup)** | 18 invariants + 18+ tests mapped |
| ✅ **Automated test selectors (synthetic canaries)** | 18 canary tests across 3 classes mapped |
| ✅ **Automated test selectors (failure drills)** | 27 drill tests across 7 classes mapped |
| ✅ **Automated test selectors (weekly digest)** | 10 invariants + 9 test classes mapped |
| ✅ **Automated test selectors (config & rollout guards)** | 7 config defaults + 3 proof-map contract tests mapped |
| ❌ **Automated test gaps (minor)** | 2 |
| 🔴 **Pending live validation (blocking)** | 7 |
| ⚪ **Pending live validation (procedural / low)** | 3 |

**Total: 440+ automated tests across 20+ test files, all CI-runnable with `FakePool`/`FakeWithingsProvider` (no live credentials required).**

---

## 13. Rollout Readiness Declaration

**⚠️ Live rollout is NOT completed and is NOT claimed by this document.**

This proof-map is an **automated evidence ledger** for CI-verifiable readiness. It documents what the code and tests prove, not what production operators have validated. The following remain explicitly **pending**:

1. **All 7 blocking live validations** (L-001 through L-007): credentials, HTTPS endpoints, vendor approval, subscribe flow verification.
2. **All 3 procedural/low validations** (L-008 through L-010): projection flag, staleness threshold policy, live revoke integration.
3. **Production dogfood:** No production users have connected Withings accounts or synced data through the live system.
4. **Vendor approval:** Withings API entitlement has not been obtained.
5. **Legal review:** No privacy or data-handling legal review has been conducted.

This document is a **build artifact**, not an operational sign-off. It is maintained alongside code and tests as part of the CI pipeline. The `test_health_release_readiness.py` contract test suite (33 tests) programmatically verifies that this document does not overclaim readiness — those tests must continue to pass on every commit.

---

## Legend

| Icon | Meaning |
|---|---|
| ✅ | Automated evidence exists in CI-runnable tests; selector(s) listed |
| ❌ | No automated test found or operator action required outside CI |
| 🔴 | Blocks production rollout until resolved |
| ⚪ | Procedural or low-priority; does not block rollout |
| ⚠️  | Minor gap with workaround; should be addressed before GA |
| 🟡 | Implemented but default-off; not yet enabled in any environment |
