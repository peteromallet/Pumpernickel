"""Fixture-backed workout normalization tests.

Covers:
- Common Withings categories → Hector taxonomy labels
- Unknown / unmapped categories
- Missing optional metrics
- Timezone and DST local-date derivation
- Revision handling
- Tombstone awareness
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from app.services.health_sync.models import (
    HealthProviderSlug,
    HealthResourceType,
    HealthSourceRecord,
    NormalizedWorkout,
    WITHINGS_WORKOUT_TAXONOMY,
    WithingsWorkoutCategory,
    HECTOR_FITNESS_TAXONOMY_LABELS,
)
from app.services.health_sync.normalization import (
    normalize_workout,
    resolve_workout_type,
    resolve_timezone,
    calculate_offset_seconds,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _make_workout_record(
    *,
    external_id: str = "workout:9102001",
    category: int | None = 2,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    timezone_name: str | None = "America/New_York",
    device_id: str | None = "synthetic-watch-device-01",
    device_model: str | None = "93",
    provider_revision: str | None = "1784492520",
    data: dict[str, Any] | None = None,
    is_deleted: bool = False,
    deleted_at: datetime | None = None,
    attribution: dict[str, Any] | None = None,
) -> HealthSourceRecord:
    """Build a minimal workout HealthSourceRecord for normalization tests."""
    if starts_at is None:
        starts_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    if ends_at is None:
        ends_at = starts_at + timedelta(minutes=41)

    source_metadata: dict[str, Any] = {
        "attrib": 7,
        "date": "2026-07-19",
    }
    if category is not None:
        source_metadata["category"] = category
    if data is not None:
        source_metadata["data"] = data

    base_attribution = {"fixture_scenario": "synthetic"}
    if attribution:
        base_attribution.update(attribution)

    return HealthSourceRecord(
        provider=HealthProviderSlug.WITHINGS,
        resource_type=HealthResourceType.WORKOUT,
        external_id=external_id,
        starts_at=starts_at,
        ends_at=ends_at,
        source_timezone=timezone_name,
        source_device_id=device_id or "",
        source_device_model=device_model or "",
        provider_revision=provider_revision,
        source_metadata=source_metadata,
        attribution=base_attribution,
        is_deleted=is_deleted,
        deleted_at=deleted_at,
    )


def _full_data() -> dict[str, Any]:
    return {
        "calories": 346,
        "distance": 6210,
        "steps": 8120,
        "elevation": 84,
        "hr_average": 146,
        "hr_max": 172,
        "hr_min": 109,
        "pause_duration": 75,
        "duration": 2460,
    }


# ── resolve_workout_type ────────────────────────────────────────────────


class TestResolveWorkoutType:
    """Unit tests for resolve_workout_type()."""

    @pytest.mark.parametrize(
        "withings_cat, expected_label",
        [
            (WithingsWorkoutCategory.WALK, "walking"),
            (WithingsWorkoutCategory.RUN, "running"),
            (WithingsWorkoutCategory.HIKING, "hiking"),
            (WithingsWorkoutCategory.SKATING, "skating"),
            (WithingsWorkoutCategory.BMX, "cycling"),
            (WithingsWorkoutCategory.BICYCLING, "cycling"),
            (WithingsWorkoutCategory.SWIMMING, "swimming"),
            (WithingsWorkoutCategory.SURFING, "surfing"),
            (WithingsWorkoutCategory.KITESURFING, "kitesurfing"),
            (WithingsWorkoutCategory.WINDSURFING, "windsurfing"),
            (WithingsWorkoutCategory.BODYBOARD, "bodyboard"),
            (WithingsWorkoutCategory.TENNIS, "tennis"),
            (WithingsWorkoutCategory.TABLE_TENNIS, "table_tennis"),
            (WithingsWorkoutCategory.SQUASH, "squash"),
            (WithingsWorkoutCategory.BADMINTON, "badminton"),
            (WithingsWorkoutCategory.LIFT_WEIGHTS, "strength"),
            (WithingsWorkoutCategory.CALISTHENICS, "strength"),
            (WithingsWorkoutCategory.ELLIPTICAL, "elliptical"),
            (WithingsWorkoutCategory.PILATES, "pilates"),
            (WithingsWorkoutCategory.BASKETBALL, "basketball"),
            (WithingsWorkoutCategory.SOCCER, "soccer"),
            (WithingsWorkoutCategory.FOOTBALL, "football"),
            (WithingsWorkoutCategory.RUGBY, "rugby"),
            (WithingsWorkoutCategory.VOLLEYBALL, "volleyball"),
            (WithingsWorkoutCategory.WATERPOLO, "waterpolo"),
            (WithingsWorkoutCategory.HORSE_RIDING, "horse_riding"),
            (WithingsWorkoutCategory.GOLF, "golf"),
            (WithingsWorkoutCategory.YOGA, "yoga"),
            (WithingsWorkoutCategory.DANCING, "dancing"),
            (WithingsWorkoutCategory.BOXING, "boxing"),
            (WithingsWorkoutCategory.FENCING, "fencing"),
            (WithingsWorkoutCategory.WRESTLING, "wrestling"),
            (WithingsWorkoutCategory.MARTIAL_ARTS, "martial_arts"),
            (WithingsWorkoutCategory.SKIING, "skiing"),
            (WithingsWorkoutCategory.SNOWBOARDING, "snowboarding"),
            (WithingsWorkoutCategory.ICE_HOCKEY, "ice_hockey"),
            (WithingsWorkoutCategory.CLIMBING, "climbing"),
            (WithingsWorkoutCategory.ICE_SKATING, "ice_skating"),
            (WithingsWorkoutCategory.MULTISPORT, "multisport"),
            (WithingsWorkoutCategory.ROWING, "rowing"),
            (WithingsWorkoutCategory.ZUMBA, "zumba"),
            (WithingsWorkoutCategory.BASEBALL, "baseball"),
            (WithingsWorkoutCategory.HANDBALL, "handball"),
            (WithingsWorkoutCategory.HOCKEY, "hockey"),
            (WithingsWorkoutCategory.PING_PONG, "table_tennis"),
            (WithingsWorkoutCategory.RIDING, "horse_riding"),
            (WithingsWorkoutCategory.ROCK_CLIMBING, "climbing"),
            (WithingsWorkoutCategory.SAILING, "sailing"),
            (WithingsWorkoutCategory.SKI_TOURING, "skiing"),
            (WithingsWorkoutCategory.SNOWSHOEING, "snowshoeing"),
            (WithingsWorkoutCategory.STAND_UP_PADDLE, "stand_up_paddle"),
            (WithingsWorkoutCategory.TRIATHLON, "triathlon"),
        ],
    )
    def test_known_category_maps_to_label(
        self, withings_cat: int, expected_label: str
    ) -> None:
        """Every Withings category in the taxonomy must map to its Hector label."""
        assert resolve_workout_type(withings_cat) == expected_label

    def test_none_category_resolves_to_unknown(self) -> None:
        """None category must resolve to 'unknown'."""
        assert resolve_workout_type(None) == "unknown"

    def test_other_category_999_resolves_to_unknown(self) -> None:
        """The OTHER category (999) is not in the taxonomy; must resolve to 'unknown'."""
        assert resolve_workout_type(999) == "unknown"
        assert 999 not in WITHINGS_WORKOUT_TAXONOMY

    def test_unmapped_category_resolves_to_unknown(self) -> None:
        """An integer not present in WITHINGS_WORKOUT_TAXONOMY must resolve to 'unknown'."""
        # Pick a value that is definitely not in the taxonomy
        assert resolve_workout_type(9999) == "unknown"
        assert resolve_workout_type(-1) == "unknown"
        assert resolve_workout_type(0) == "unknown"

    def test_every_taxonomy_label_is_a_valid_hector_label(self) -> None:
        """Every value in WITHINGS_WORKOUT_TAXONOMY must be a known string."""
        labels = set(WITHINGS_WORKOUT_TAXONOMY.values())
        assert "unknown" not in labels  # unknown is reserved for unmapped
        assert all(isinstance(label, str) and label for label in labels)
        # At minimum the fitness taxonomy labels should be a subset
        assert HECTOR_FITNESS_TAXONOMY_LABELS.issubset(labels)


# ── normalize_workout: common categories ─────────────────────────────────


class TestNormalizeWorkoutCommonCategories:
    """normalize_workout() must correctly decode workouts from common categories."""

    def test_running_workout_with_full_metrics(self) -> None:
        record = _make_workout_record(category=2, data=_full_data())
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "running"
        assert result.duration_seconds == 2460
        assert result.pause_duration_seconds == 75
        assert result.distance_meters == pytest.approx(6210.0)
        assert result.steps == 8120
        assert result.energy_kcal == pytest.approx(346.0)
        assert result.elevation_gain_meters == pytest.approx(84.0)
        assert result.average_heart_rate_bpm == pytest.approx(146.0)
        assert result.max_heart_rate_bpm == pytest.approx(172.0)
        assert result.source_device_id == "synthetic-watch-device-01"
        assert result.source_device_model == "93"
        assert result.local_timezone == "America/New_York"
        assert result.local_date is not None
        assert result.attribution["provider_category"] == 2

    def test_walking_workout(self) -> None:
        record = _make_workout_record(
            category=1,
            data={"calories": 120, "distance": 3200, "steps": 4500, "duration": 1800},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "walking"
        assert result.duration_seconds == 1800
        assert result.distance_meters == pytest.approx(3200.0)
        assert result.steps == 4500

    def test_cycling_workout(self) -> None:
        record = _make_workout_record(
            category=6,
            data={"calories": 520, "distance": 25000, "duration": 3600, "elevation": 320},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "cycling"
        assert result.distance_meters == pytest.approx(25000.0)
        assert result.elevation_gain_meters == pytest.approx(320.0)

    def test_swimming_workout(self) -> None:
        record = _make_workout_record(
            category=7,
            data={"calories": 280, "distance": 1500, "duration": 2400, "hr_average": 135},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "swimming"
        assert result.distance_meters == pytest.approx(1500.0)
        assert result.average_heart_rate_bpm == pytest.approx(135.0)

    def test_strength_workout_via_lift_weights(self) -> None:
        """LIFT_WEIGHTS (16) must map to 'strength'."""
        record = _make_workout_record(
            category=16,
            data={"calories": 180, "duration": 2700, "hr_average": 120},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "strength"

    def test_strength_workout_via_calisthenics(self) -> None:
        """CALISTHENICS (17) must map to 'strength'."""
        record = _make_workout_record(
            category=17,
            data={"calories": 200, "duration": 2400},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "strength"

    def test_yoga_workout(self) -> None:
        record = _make_workout_record(
            category=28,
            data={"calories": 95, "duration": 3600},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "yoga"

    def test_hiking_workout(self) -> None:
        record = _make_workout_record(
            category=3,
            data={"calories": 450, "distance": 8500, "elevation": 420, "duration": 5400},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "hiking"

    def test_skiing_workout(self) -> None:
        record = _make_workout_record(
            category=34,
            data={"calories": 600, "distance": 12000, "elevation": 2500, "duration": 7200},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "skiing"

    def test_triathlon_workout(self) -> None:
        record = _make_workout_record(
            category=52,
            data={
                "calories": 2500,
                "distance": 51500,
                "duration": 10800,
                "hr_average": 155,
                "hr_max": 185,
            },
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "triathlon"


# ── normalize_workout: unknown categories ────────────────────────────────


class TestNormalizeWorkoutUnknownCategory:
    """normalize_workout() with unknown / unmapped categories."""

    def test_unknown_category_from_none_source_metadata(self) -> None:
        """When source_metadata has no 'category' key, resolve to 'unknown'."""
        record = _make_workout_record(category=None, data={"calories": 100, "duration": 600})
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "unknown"
        assert result.attribution["provider_category"] is None

    def test_unknown_category_999_other(self) -> None:
        """Category 999 (OTHER) is not in the taxonomy."""
        record = _make_workout_record(category=999, data={"calories": 150, "duration": 900})
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "unknown"

    def test_unknown_category_unmapped_integer(self) -> None:
        """An integer not in WITHINGS_WORKOUT_TAXONOMY resolves to 'unknown'."""
        record = _make_workout_record(category=9999, data={"calories": 100, "duration": 600})
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "unknown"

    def test_unknown_category_does_not_block_other_metrics(self) -> None:
        """Even with unknown category, other metrics must still be decoded."""
        record = _make_workout_record(
            category=999,
            data={"calories": 320, "distance": 5000, "duration": 1800, "hr_average": 140},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "unknown"
        assert result.energy_kcal == pytest.approx(320.0)
        assert result.distance_meters == pytest.approx(5000.0)
        assert result.duration_seconds == 1800
        assert result.average_heart_rate_bpm == pytest.approx(140.0)


# ── normalize_workout: missing optional metrics ──────────────────────────


class TestNormalizeWorkoutMissingOptional:
    """normalize_workout() must handle missing optional fields gracefully."""

    def test_no_data_field_at_all(self) -> None:
        """When source_metadata has no 'data' key, all optional metrics are None."""
        record = _make_workout_record(category=2, data=None)
        result = normalize_workout(record)
        assert result is not None
        assert result.workout_type == "running"
        # Duration derived from start-end delta
        assert result.duration_seconds is not None  # computed from starts_at/ends_at
        assert result.distance_meters is None
        assert result.steps is None
        assert result.energy_kcal is None
        assert result.elevation_gain_meters is None
        assert result.average_heart_rate_bpm is None
        assert result.max_heart_rate_bpm is None
        assert result.pause_duration_seconds is None

    def test_empty_data_dict(self) -> None:
        """Empty data dict should yield None for all optional metrics."""
        record = _make_workout_record(category=2, data={})
        result = normalize_workout(record)
        assert result is not None
        assert result.distance_meters is None
        assert result.steps is None
        assert result.energy_kcal is None
        assert result.average_heart_rate_bpm is None
        assert result.max_heart_rate_bpm is None
        assert result.pause_duration_seconds is None
        assert result.elevation_gain_meters is None

    def test_partial_data_fields(self) -> None:
        """Only some data fields present → missing ones are None."""
        record = _make_workout_record(
            category=2,
            data={"calories": 200, "duration": 900},  # no distance, steps, hr, etc.
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.energy_kcal == pytest.approx(200.0)
        assert result.duration_seconds == 900
        assert result.distance_meters is None
        assert result.steps is None
        assert result.average_heart_rate_bpm is None

    def test_missing_device_id_and_model(self) -> None:
        """When device info is absent, fields are None/empty."""
        record = _make_workout_record(
            category=2,
            device_id=None,
            device_model=None,
            data={"calories": 100, "duration": 600},
        )
        result = normalize_workout(record)
        assert result is not None
        # source_device_id should be None when record had empty string
        # (the normalizer uses `record.source_device_id or None`)
        assert result.source_device_id is None
        assert result.source_device_model is None

    def test_missing_timezone_falls_back_to_utc_date(self) -> None:
        """When source_timezone is None, local_date derived from UTC started_at."""
        record = _make_workout_record(
            category=2,
            timezone_name=None,
            data={"calories": 100, "duration": 600},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.local_timezone is None
        assert result.local_offset_seconds is None
        # local_date should fall back to started_at.date() in UTC
        assert result.local_date == record.starts_at.date()

    def test_missing_starts_at_returns_none(self) -> None:
        """Workouts without starts_at cannot be normalized."""
        # Cannot use _make_workout_record because it applies a default for starts_at.
        record = HealthSourceRecord(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.WORKOUT,
            external_id="workout:9102001",
            starts_at=None,
            ends_at=datetime(2026, 7, 19, 12, 30, tzinfo=UTC),
            source_timezone="America/New_York",
            source_device_id="dev01",
            source_device_model="93",
            source_metadata={"attrib": 7, "category": 2, "date": "2026-07-19", "data": {"calories": 100}},
            attribution={"fixture_scenario": "synthetic"},
        )
        result = normalize_workout(record)
        assert result is None

    def test_duration_computed_from_start_end_when_not_in_data(self) -> None:
        """When 'duration' is absent from data, compute from starts_at - ends_at."""
        record = _make_workout_record(
            category=2,
            starts_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
            ends_at=datetime(2026, 7, 19, 12, 30, tzinfo=UTC),
            data={"calories": 200},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.duration_seconds == 1800  # 30 minutes


# ── normalize_workout: timezone and DST local-date ───────────────────────


class TestNormalizeWorkoutTimezoneDST:
    """local_date and offset derivation including DST transitions."""

    def test_local_date_from_eastern_timezone(self) -> None:
        """started_at in America/New_York should derive correct local_date."""
        # 2026-07-19 12:00 UTC = 2026-07-19 08:00 EDT (UTC-4)
        record = _make_workout_record(
            category=2,
            starts_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
            timezone_name="America/New_York",
            data={"calories": 200, "duration": 1800},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.local_date == date(2026, 7, 19)
        assert result.local_timezone == "America/New_York"
        # EDT is UTC-4
        assert result.local_offset_seconds == -14400

    def test_local_date_during_standard_time(self) -> None:
        """During EST (UTC-5), offset should be -18000."""
        # 2026-01-15 18:00 UTC = 2026-01-15 13:00 EST
        record = _make_workout_record(
            category=2,
            starts_at=datetime(2026, 1, 15, 18, 0, tzinfo=UTC),
            ends_at=datetime(2026, 1, 15, 19, 0, tzinfo=UTC),
            timezone_name="America/New_York",
            data={"calories": 200, "duration": 3600},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.local_date == date(2026, 1, 15)
        assert result.local_offset_seconds == -18000  # EST

    def test_dst_spring_forward_local_date_derivation(self) -> None:
        """Workout that starts before DST spring-forward."""
        # 2026-03-08 06:30 UTC = 2026-03-08 01:30 EST (before transition)
        starts_at = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
        ends_at = datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
        record = _make_workout_record(
            category=2,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone_name="America/New_York",
            data={"calories": 300, "duration": 5400},
        )
        result = normalize_workout(record)
        assert result is not None
        # local_date derived from started_at = 01:30 EST → 2026-03-08
        assert result.local_date == date(2026, 3, 8)
        # Offset at start time (EST = UTC-5)
        assert result.local_offset_seconds == -18000

    def test_dst_spring_forward_after_transition(self) -> None:
        """Workout that starts after DST spring-forward transition."""
        # 2026-03-08 08:00 UTC = 2026-03-08 04:00 EDT (after transition)
        starts_at = datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
        record = _make_workout_record(
            category=2,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            timezone_name="America/New_York",
            data={"calories": 200, "duration": 3600},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.local_date == date(2026, 3, 8)
        # Offset after transition (EDT = UTC-4)
        assert result.local_offset_seconds == -14400

    def test_dst_fall_back_local_date_derivation(self) -> None:
        """Workout spanning DST fall-back transition."""
        # 2026-11-01 05:30 UTC = 2026-11-01 01:30 EDT (before fall-back)
        starts_at = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        record = _make_workout_record(
            category=2,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            timezone_name="America/New_York",
            data={"calories": 400, "duration": 7200},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.local_date == date(2026, 11, 1)
        # Offset at start time (EDT = UTC-4, before transition)
        assert result.local_offset_seconds == -14400

    def test_western_european_timezone(self) -> None:
        """Europe/Paris: UTC+2 in summer."""
        # 2026-07-19 06:00 UTC = 08:00 CEST
        record = _make_workout_record(
            category=2,
            starts_at=datetime(2026, 7, 19, 6, 0, tzinfo=UTC),
            timezone_name="Europe/Paris",
            data={"calories": 250, "duration": 3600},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.local_date == date(2026, 7, 19)
        assert result.local_offset_seconds == 7200  # UTC+2

    def test_western_european_winter_time(self) -> None:
        """Europe/Paris: UTC+1 in winter."""
        # 2026-01-15 07:00 UTC = 08:00 CET
        record = _make_workout_record(
            category=2,
            starts_at=datetime(2026, 1, 15, 7, 0, tzinfo=UTC),
            timezone_name="Europe/Paris",
            data={"calories": 250, "duration": 3600},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.local_date == date(2026, 1, 15)
        assert result.local_offset_seconds == 3600  # UTC+1

    def test_invalid_timezone_fallback_to_utc_date(self) -> None:
        """Invalid timezone name → fall back to UTC date, offset None."""
        record = _make_workout_record(
            category=2,
            starts_at=datetime(2026, 7, 19, 23, 0, tzinfo=UTC),
            timezone_name="Not/A_Real_Zone",
            data={"calories": 100, "duration": 600},
        )
        result = normalize_workout(record)
        assert result is not None
        # UTC date is 2026-07-19
        assert result.local_date == date(2026, 7, 19)
        assert result.local_timezone == "Not/A_Real_Zone"
        assert result.local_offset_seconds is None

    def test_empty_timezone_fallback_to_utc_date(self) -> None:
        """Empty/whitespace timezone → fall back to UTC date, offset None."""
        record = _make_workout_record(
            category=2,
            starts_at=datetime(2026, 7, 20, 1, 0, tzinfo=UTC),
            timezone_name="   ",
            data={"calories": 100, "duration": 600},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.local_date == date(2026, 7, 20)
        assert result.local_offset_seconds is None


# ── normalize_workout: revisions ─────────────────────────────────────────


class TestNormalizeWorkoutRevisions:
    """Revision tracking in normalized workouts."""

    def test_revision_count_stored_in_attribution(self) -> None:
        """revision_count is threaded through to attribution dict."""
        record = _make_workout_record(
            category=2,
            data={"calories": 200, "duration": 1800},
        )
        result = normalize_workout(record, revision_count=3)
        assert result is not None
        assert result.attribution["revision_count"] == 3

    def test_default_revision_count_is_one(self) -> None:
        """When not specified, revision_count defaults to 1."""
        record = _make_workout_record(
            category=2,
            data={"calories": 200, "duration": 1800},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.attribution["revision_count"] == 1

    def test_provider_category_in_attribution(self) -> None:
        """provider_category is included in attribution for auditability."""
        record = _make_workout_record(
            category=34,  # SKIING
            data={"calories": 500, "duration": 3600},
        )
        result = normalize_workout(record, revision_count=2)
        assert result is not None
        assert result.attribution["provider_category"] == 34
        assert result.attribution["revision_count"] == 2

    def test_base_attribution_preserved(self) -> None:
        """Existing attribution keys are preserved alongside revision metadata."""
        record = _make_workout_record(
            category=2,
            data={"calories": 200, "duration": 1800},
            attribution={"fixture_scenario": "workouts_revision", "custom_key": "custom_value"},
        )
        result = normalize_workout(record, revision_count=5)
        assert result is not None
        assert result.attribution["fixture_scenario"] == "workouts_revision"
        assert result.attribution["custom_key"] == "custom_value"
        assert result.attribution["revision_count"] == 5
        assert result.attribution["provider_category"] == 2


# ── normalize_workout: tombstones ────────────────────────────────────────


class TestNormalizeWorkoutTombstones:
    """normalize_workout() must return None for deleted/non-workout records."""

    def test_deleted_workout_returns_none(self) -> None:
        """A deleted workout must not produce a normalized row."""
        record = _make_workout_record(
            category=2,
            data={"calories": 200, "duration": 1800},
            is_deleted=True,
            deleted_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        result = normalize_workout(record)
        assert result is None

    def test_non_workout_record_returns_none(self) -> None:
        """Only WORKOUT records are processed; others return None."""
        record = HealthSourceRecord(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.MEASUREMENT,
            external_id="grpid:9001001",
            starts_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        )
        result = normalize_workout(record)
        assert result is None

    def test_sleep_record_returns_none(self) -> None:
        """Sleep records must not produce workout normalized rows."""
        record = HealthSourceRecord(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.SLEEP,
            external_id="sleep_summary:9203001",
            starts_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        )
        result = normalize_workout(record)
        assert result is None


# ── normalize_workout: attribution & source identity ─────────────────────


class TestNormalizeWorkoutAttribution:
    """Attribution and provider source identity on normalized workouts."""

    def test_fixture_scenario_preserved(self) -> None:
        """The fixture_scenario key in attribution must survive normalization."""
        record = _make_workout_record(
            category=2,
            data={"calories": 200, "duration": 1800},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.attribution["fixture_scenario"] == "synthetic"

    def test_device_info_on_normalized_workout(self) -> None:
        """Device ID and model must be transferred to the normalized record."""
        record = _make_workout_record(
            category=28,
            device_id="yoga-device-42",
            device_model="YogaMat Pro",
            data={"calories": 50, "duration": 2700},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.source_device_id == "yoga-device-42"
        assert result.source_device_model == "YogaMat Pro"

    def test_started_at_and_ended_at_transferred(self) -> None:
        """started_at and ended_at must be exact copies on the normalized record."""
        starts = datetime(2026, 7, 19, 10, 30, tzinfo=UTC)
        ends = datetime(2026, 7, 19, 11, 15, tzinfo=UTC)
        record = _make_workout_record(
            category=2,
            starts_at=starts,
            ends_at=ends,
            data={"calories": 250},
        )
        result = normalize_workout(record)
        assert result is not None
        assert result.started_at == starts
        assert result.ended_at == ends

    def test_normalized_workout_is_frozen_dataclass(self) -> None:
        """NormalizedWorkout must be a frozen dataclass (immutable)."""
        result = normalize_workout(_make_workout_record(category=2, data={"duration": 600}))
        assert result is not None
        with pytest.raises(Exception):
            result.distance_meters = 100.0  # type: ignore[misc]

    def test_ended_at_earlier_than_started_at_raises(self) -> None:
        """NormalizedWorkout constructor must reject invalid time intervals."""
        with pytest.raises(ValueError, match="ended_at must not be earlier than started_at"):
            NormalizedWorkout(
                started_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
                ended_at=datetime(2026, 7, 19, 11, 0, tzinfo=UTC),
                workout_type="running",
            )


# ── resolve_timezone / calculate_offset_seconds ──────────────────────────


class TestTimezoneHelpers:
    """Pure timezone helper function coverage."""

    def test_resolve_valid_timezone(self) -> None:
        tz = resolve_timezone("America/New_York")
        assert tz is not None

    def test_resolve_none_timezone(self) -> None:
        assert resolve_timezone(None) is None

    def test_resolve_empty_timezone(self) -> None:
        assert resolve_timezone("") is None
        assert resolve_timezone("   ") is None

    def test_resolve_invalid_timezone(self) -> None:
        assert resolve_timezone("Not/A_Zone") is None

    def test_calculate_offset_valid(self) -> None:
        offset = calculate_offset_seconds(
            "America/New_York",
            at_datetime=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        )
        assert offset == -14400  # EDT

    def test_calculate_offset_none_timezone(self) -> None:
        assert calculate_offset_seconds(None) is None

    def test_calculate_offset_invalid_timezone(self) -> None:
        assert calculate_offset_seconds("Bad/Zone") is None

    def test_calculate_offset_defaults_to_now(self) -> None:
        offset = calculate_offset_seconds("UTC")
        assert offset == 0

    def test_calculate_offset_naive_datetime_assumes_utc(self) -> None:
        offset = calculate_offset_seconds(
            "Europe/Paris",
            at_datetime=datetime(2026, 7, 19, 12, 0),  # naive → assumed UTC
        )
        # July: CEST = UTC+2
        assert offset == 7200
