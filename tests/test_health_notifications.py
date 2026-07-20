from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.health_sync.notifications import (
    WithingsNotificationError,
    ingest_withings_notification,
)
from tests.conftest import FakePool


@pytest.mark.asyncio
async def test_ingest_notification_deduplicates_and_marks_dirty() -> None:
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id, external_user_id="420001")
    form = {"userid": "420001", "appli": "1", "date": "1721472000"}

    first = await ingest_withings_notification(
        pool,
        content_type="application/x-www-form-urlencoded",
        form=form,
    )
    second = await ingest_withings_notification(
        pool,
        content_type="application/x-www-form-urlencoded; charset=utf-8",
        form=form,
    )

    assert first.status == "queued"
    assert second.status == "deduplicated"
    assert len(pool.health_webhook_receipts) == 1
    assert len(pool.health_dirty_categories) == 1


@pytest.mark.asyncio
async def test_ingest_notification_ignores_unknown_connection() -> None:
    pool = FakePool()

    result = await ingest_withings_notification(
        pool,
        content_type="application/x-www-form-urlencoded",
        form={"userid": "999999", "appli": "44", "date": "1721472000"},
    )

    assert result.status == "ignored"
    assert len(pool.health_webhook_receipts) == 1
    receipt = next(iter(pool.health_webhook_receipts.values()))
    assert receipt["connection_id"] is None
    assert receipt["status"] == "ignored"
    assert pool.health_dirty_categories == {}


@pytest.mark.asyncio
async def test_ingest_notification_rejects_invalid_payloads() -> None:
    pool = FakePool()

    with pytest.raises(WithingsNotificationError) as wrong_content_type:
        await ingest_withings_notification(
            pool,
            content_type="application/json",
            form={"userid": "420001", "appli": "1"},
        )
    assert wrong_content_type.value.status_code == 415

    with pytest.raises(WithingsNotificationError) as missing_user:
        await ingest_withings_notification(
            pool,
            content_type="application/x-www-form-urlencoded",
            form={"appli": "1"},
        )
    assert missing_user.value.status_code == 400

    with pytest.raises(WithingsNotificationError) as unsupported_category:
        await ingest_withings_notification(
            pool,
            content_type="application/x-www-form-urlencoded",
            form={"userid": "420001", "appli": "999"},
        )
    assert unsupported_category.value.status_code == 400
