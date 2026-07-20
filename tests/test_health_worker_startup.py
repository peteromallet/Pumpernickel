from __future__ import annotations

import asyncio
import base64
import contextlib
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from pydantic import ValidationError

from app.config import get_settings
from tests.conftest import FakePool


@contextlib.asynccontextmanager
async def _noop_db_lifespan(app: Any) -> AsyncIterator[None]:
    yield


async def _idle_forever(*args, **kwargs) -> None:
    await asyncio.Event().wait()


async def _noop_async(*args, **kwargs) -> None:
    return None


class _FakeApp:
    def __init__(self) -> None:
        self.state = SimpleNamespace(pool=FakePool())


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _clear_health_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "HEALTH_SYNC_ENABLED",
        "HEALTH_SYNC_MEASUREMENTS_ENABLED",
        "HEALTH_SYNC_WORKOUTS_ENABLED",
        "HEALTH_SYNC_SLEEP_ENABLED",
        "HEALTH_SYNC_POLL_INTERVAL_S",
        "HEALTH_SYNC_BATCH_SIZE",
        "HEALTH_SYNC_REQUEST_TIMEOUT_S",
        "HEALTH_SYNC_MAX_ATTEMPTS",
        "HEALTH_SYNC_RETRY_AFTER_CAP_SECONDS",
        "HEALTH_SYNC_RECONCILIATION_INTERVAL_S",
        "DATA_ENCRYPTION_KEY",
        "WITHINGS_CLIENT_ID",
        "WITHINGS_CLIENT_SECRET",
        "WITHINGS_CALLBACK_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def _set_health_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALTH_SYNC_ENABLED", "true")
    monkeypatch.setenv("HEALTH_SYNC_MEASUREMENTS_ENABLED", "true")
    monkeypatch.setenv(
        "DATA_ENCRYPTION_KEY",
        base64.b64encode(b"0123456789abcdef0123456789abcdef").decode(),
    )
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "client-id")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "WITHINGS_CALLBACK_URL",
        "https://example.test/api/health/devices/withings/oauth/callback",
    )


def _stub_lifespan_dependencies(main_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "db_lifespan", _noop_db_lifespan)
    monkeypatch.setattr(main_mod, "_log_startup_diagnostics", lambda: None)
    monkeypatch.setattr(main_mod, "recover_on_startup", _noop_async)
    monkeypatch.setattr(main_mod, "run_recovery_forever", _idle_forever)
    monkeypatch.setattr(main_mod, "run_metrics_sweep_forever", _idle_forever)
    monkeypatch.setattr(main_mod, "seed_heartbeat", _noop_async)
    monkeypatch.setattr(main_mod, "seed_weekly_reflections", _noop_async)
    monkeypatch.setattr(main_mod.agentic, "set_pool", lambda pool: None)
    monkeypatch.setattr(main_mod.hooks, "set_pool", lambda pool: None)
    monkeypatch.setattr(main_mod.whatsapp, "init_client", _noop_async)
    monkeypatch.setattr(main_mod.whatsapp, "close_client", _noop_async)
    monkeypatch.setattr(main_mod.discord, "close_all_clients", _noop_async)

    from app.bots import registry as registry_mod

    monkeypatch.setattr(registry_mod, "populate_mediator_spec_from_db", _noop_async)
    monkeypatch.setattr(registry_mod, "populate_tante_rosi_spec_from_db", _noop_async)
    monkeypatch.setattr(registry_mod, "populate_hector_spec_from_db", _noop_async)
    monkeypatch.setattr(registry_mod, "populate_habits_spec_from_db", _noop_async)
    monkeypatch.setattr(registry_mod, "populate_superpom_spec_from_db", _noop_async)
    monkeypatch.setattr(registry_mod, "populate_topic_ids_from_db", _noop_async)


@pytest.mark.asyncio
async def test_lifespan_starts_with_health_flags_off_without_provider_credentials(
    app_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_mod

    _clear_health_env(monkeypatch)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    _stub_lifespan_dependencies(main_mod, monkeypatch)
    app = _FakeApp()

    async with main_mod.lifespan(app):
        assert not hasattr(app.state, "health_sync_worker")
        assert len(app.state.background_tasks) == 2


@pytest.mark.asyncio
async def test_lifespan_fails_closed_when_health_sync_enabled_without_required_credentials(
    app_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_mod

    _clear_health_env(monkeypatch)
    monkeypatch.setenv("HEALTH_SYNC_ENABLED", "true")
    monkeypatch.setenv("HEALTH_SYNC_MEASUREMENTS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setattr(main_mod, "db_lifespan", _noop_db_lifespan)

    with pytest.raises(
        ValidationError,
        match=(
            "HEALTH_SYNC_ENABLED requires DATA_ENCRYPTION_KEY, "
            "WITHINGS_CLIENT_ID, WITHINGS_CLIENT_SECRET, WITHINGS_CALLBACK_URL"
        ),
    ):
        async with main_mod.lifespan(_FakeApp()):
            pass


@pytest.mark.asyncio
async def test_lifespan_wires_health_sync_worker_when_enabled(
    app_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_mod

    _clear_health_env(monkeypatch)
    _set_health_enabled_env(monkeypatch)
    monkeypatch.setenv("HEALTH_SYNC_POLL_INTERVAL_S", "17")
    monkeypatch.setenv("HEALTH_SYNC_BATCH_SIZE", "9")
    monkeypatch.setenv("HEALTH_SYNC_REQUEST_TIMEOUT_S", "8")
    monkeypatch.setenv("HEALTH_SYNC_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("HEALTH_SYNC_RETRY_AFTER_CAP_SECONDS", "21")
    monkeypatch.setenv("HEALTH_SYNC_RECONCILIATION_INTERVAL_S", "600")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    _stub_lifespan_dependencies(main_mod, monkeypatch)

    created: list[object] = []

    class _StubHealthSyncWorker:
        def __init__(self, pool, *, settings=None, provider=None) -> None:
            self.pool = pool
            self.settings = settings
            self.provider = provider
            created.append(self)

        async def run_forever(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(main_mod, "HealthSyncWorker", _StubHealthSyncWorker)
    app = _FakeApp()

    async with main_mod.lifespan(app):
        assert len(created) == 1
        worker = created[0]
        assert app.state.health_sync_worker is worker
        assert worker.pool is app.state.pool
        assert worker.provider is None
        assert worker.settings.health_sync_poll_interval_s == 17.0
        assert worker.settings.health_sync_batch_size == 9
        assert worker.settings.health_sync_request_timeout_s == 8.0
        assert worker.settings.health_sync_max_attempts == 4
        assert worker.settings.health_sync_retry_after_cap_seconds == 21
        assert worker.settings.health_sync_reconciliation_interval_s == 600.0
