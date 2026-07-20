import pytest
from pydantic import ValidationError

from app.config import Settings


def _clear_health_sync_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "HEALTH_SYNC_ENABLED",
        "HEALTH_SYNC_MEASUREMENTS_ENABLED",
        "HEALTH_SYNC_WORKOUTS_ENABLED",
        "HEALTH_SYNC_SLEEP_ENABLED",
        "DATA_ENCRYPTION_KEY",
        "WITHINGS_CLIENT_ID",
        "WITHINGS_CLIENT_SECRET",
        "WITHINGS_CALLBACK_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_health_sync_defaults_off_and_starts_without_provider_secrets(app_env, monkeypatch) -> None:
    _clear_health_sync_env(monkeypatch)

    settings = Settings()

    assert settings.health_sync_enabled is False
    assert settings.health_sync_measurements_enabled is False
    assert settings.health_sync_workouts_enabled is False
    assert settings.health_sync_sleep_enabled is False
    assert settings.data_encryption_key is None
    assert settings.withings_client_id is None
    assert settings.withings_client_secret is None
    assert settings.withings_callback_url == ""


def test_health_subfeature_flags_do_not_require_secrets_when_master_flag_is_off(app_env, monkeypatch) -> None:
    _clear_health_sync_env(monkeypatch)
    monkeypatch.setenv("HEALTH_SYNC_MEASUREMENTS_ENABLED", "true")
    monkeypatch.setenv("HEALTH_SYNC_WORKOUTS_ENABLED", "true")
    monkeypatch.setenv("HEALTH_SYNC_SLEEP_ENABLED", "true")

    settings = Settings()

    assert settings.health_sync_enabled is False
    assert settings.health_sync_measurements_enabled is True
    assert settings.health_sync_workouts_enabled is True
    assert settings.health_sync_sleep_enabled is True


@pytest.mark.parametrize(
    ("missing_env", "expected_token"),
    [
        ("DATA_ENCRYPTION_KEY", "DATA_ENCRYPTION_KEY"),
        ("WITHINGS_CLIENT_ID", "WITHINGS_CLIENT_ID"),
        ("WITHINGS_CLIENT_SECRET", "WITHINGS_CLIENT_SECRET"),
        ("WITHINGS_CALLBACK_URL", "WITHINGS_CALLBACK_URL"),
    ],
)
def test_health_sync_enabled_requires_each_secret_and_callback(
    app_env,
    monkeypatch,
    missing_env: str,
    expected_token: str,
) -> None:
    _clear_health_sync_env(monkeypatch)
    monkeypatch.setenv("HEALTH_SYNC_ENABLED", "true")
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "client-id")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "WITHINGS_CALLBACK_URL",
        "https://example.test/api/health/devices/withings/oauth/callback",
    )
    monkeypatch.delenv(missing_env, raising=False)

    with pytest.raises(ValidationError, match=expected_token):
        Settings()


def test_health_sync_enabled_reports_all_missing_inputs_deterministically(app_env, monkeypatch) -> None:
    _clear_health_sync_env(monkeypatch)
    monkeypatch.setenv("HEALTH_SYNC_ENABLED", "true")

    with pytest.raises(
        ValidationError,
        match=(
            "HEALTH_SYNC_ENABLED requires DATA_ENCRYPTION_KEY, "
            "WITHINGS_CLIENT_ID, WITHINGS_CLIENT_SECRET, WITHINGS_CALLBACK_URL"
        ),
    ):
        Settings()


@pytest.mark.parametrize(
    "callback_url",
    [
        "https://example.test/api/health/devices/withings/oauth/callback?code=1",
        "https://example.test/api/health/devices/withings/oauth/callback#fragment",
        "https://example.test/api/health/devices/withings/oauth/callback/",
        "https://example.test/api/health/devices/withings/oauth/wrong",
        "/api/health/devices/withings/oauth/callback",
    ],
)
def test_health_sync_enabled_requires_exact_callback_url(
    app_env,
    monkeypatch,
    callback_url: str,
) -> None:
    _clear_health_sync_env(monkeypatch)
    monkeypatch.setenv("HEALTH_SYNC_ENABLED", "true")
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "client-id")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("WITHINGS_CALLBACK_URL", callback_url)

    with pytest.raises(
        ValidationError,
        match=(
            "WITHINGS_CALLBACK_URL must be an absolute URL with exact path "
            "/api/health/devices/withings/oauth/callback and no query or fragment"
        ),
    ):
        Settings()


def test_health_sync_enabled_accepts_exact_callback_url(app_env, monkeypatch) -> None:
    _clear_health_sync_env(monkeypatch)
    monkeypatch.setenv("HEALTH_SYNC_ENABLED", "true")
    monkeypatch.setenv("HEALTH_SYNC_MEASUREMENTS_ENABLED", "true")
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "client-id")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "WITHINGS_CALLBACK_URL",
        "https://example.test/api/health/devices/withings/oauth/callback",
    )

    settings = Settings()

    assert settings.health_sync_enabled is True
    assert settings.health_sync_measurements_enabled is True
    assert settings.withings_callback_url == (
        "https://example.test/api/health/devices/withings/oauth/callback"
    )
