from __future__ import annotations

import json
from pathlib import Path


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "withings"


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def test_withings_fixture_catalog_covers_required_scenarios() -> None:
    catalog = _load_json(FIXTURE_DIR / "catalog.json")

    assert catalog["provider"] == "withings"
    assert catalog["data_classification"] == "synthetic"
    assert catalog["contains_live_credentials"] is False

    scenarios = catalog["scenarios"]
    assert isinstance(scenarios, list)

    scenario_ids = {scenario["id"] for scenario in scenarios}
    assert scenario_ids == {
        "oauth_token_exchange_success",
        "oauth_token_refresh_rotated",
        "measurements_page_1",
        "measurements_page_2",
        "measurements_revision",
        "measurements_tombstones",
        "workouts_page_1",
        "sleep_summary_page_1",
        "sleep_detail_page_1",
        "rate_limit_retry_after",
        "malformed_measurements_body",
        "transient_service_unavailable",
        "request_timeout",
    }

    all_tags = {tag for scenario in scenarios for tag in scenario["tags"]}
    assert {
        "happy_path",
        "pagination",
        "revision",
        "tombstone",
        "token_rotation",
        "rate_limit",
        "malformed_response",
        "timeout",
        "transient_error",
        "sleep_summary",
        "sleep_detail",
    } <= all_tags


def test_withings_fixture_files_match_catalog_and_stay_sanitized() -> None:
    catalog = _load_json(FIXTURE_DIR / "catalog.json")
    banned_fragments = {
        "veas-production.up.railway.app",
        "account.withings.com",
        "wbsapi.withings.net",
        "@withings.com",
    }

    for scenario in catalog["scenarios"]:
        path = FIXTURE_DIR / scenario["file"]
        assert path.exists(), path

        payload = _load_json(path)
        assert payload["scenario_id"] == scenario["id"]
        assert payload["origin"] == "synthetic"

        dumped = json.dumps(payload, sort_keys=True)
        assert "synthetic-" in dumped or "example.test" in dumped or "grpid:" in dumped
        for banned in banned_fragments:
            assert banned not in dumped


def test_withings_fixture_behavior_examples_cover_pagination_rotation_and_errors() -> None:
    page_1 = _load_json(FIXTURE_DIR / "measurements_page_1.json")
    page_2 = _load_json(FIXTURE_DIR / "measurements_page_2.json")
    revision = _load_json(FIXTURE_DIR / "measurements_revision.json")
    refresh = _load_json(FIXTURE_DIR / "oauth_token_refresh_rotated.json")
    rate_limit = _load_json(FIXTURE_DIR / "rate_limit_retry_after.json")
    malformed = _load_json(FIXTURE_DIR / "malformed_measurements_body.json")
    timeout = _load_json(FIXTURE_DIR / "request_timeout.json")
    sleep_summary = _load_json(FIXTURE_DIR / "sleep_summary_page_1.json")
    sleep_detail = _load_json(FIXTURE_DIR / "sleep_detail_page_1.json")

    assert page_1["response"]["json"]["body"]["more"] == 1
    assert page_1["response"]["json"]["body"]["offset"] == 100
    assert page_2["response"]["json"]["body"]["more"] == 0
    assert page_2["response"]["json"]["body"]["offset"] == 0

    revised_group = revision["response"]["json"]["body"]["measuregrps"][0]
    assert revised_group["grpid"] == 9001002
    assert revised_group["modified"] > page_2["response"]["json"]["body"]["measuregrps"][0]["modified"]

    assert (
        refresh["request"]["form"]["refresh_token"]
        != refresh["response"]["json"]["body"]["refresh_token"]
    )
    assert refresh["response"]["json"]["body"]["access_token"] == "synthetic-access-token-v2"

    assert rate_limit["response"]["status_code"] == 429
    assert rate_limit["response"]["headers"]["Retry-After"] == "120"
    assert rate_limit["response"]["json"]["status"] == 601

    assert malformed["response"]["body_text"].startswith("{\"status\":0")
    assert timeout["transport_error"]["kind"] == "timeout"

    summary_series = sleep_summary["response"]["json"]["body"]["series"][0]
    assert summary_series["data"]["sleep_score"] == 83
    assert sleep_detail["response"]["json"]["body"]["series"]["hr"]["1784473200"] == 58
