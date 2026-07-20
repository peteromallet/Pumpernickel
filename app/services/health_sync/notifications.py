"""Withings webhook validation and durable dirty-category queueing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping

from app.services.health_sync.models import HealthProviderSlug, HealthResourceType
from app.services.health_sync.repository import repository_for

_WITHINGS_WEBHOOK_CONTENT_TYPE = "application/x-www-form-urlencoded"
_WITHINGS_CATEGORY_MAP: dict[int, HealthResourceType] = {
    1: HealthResourceType.MEASUREMENT,
    16: HealthResourceType.WORKOUT,
    44: HealthResourceType.SLEEP,
    50: HealthResourceType.SLEEP,
    51: HealthResourceType.SLEEP,
    52: HealthResourceType.SLEEP,
}


class WithingsNotificationError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class WithingsNotificationResult:
    status: str
    resource_type: HealthResourceType | None = None


def _normalized_content_type(value: str | None) -> str:
    if value is None:
        return ""
    return value.split(";", 1)[0].strip().casefold()


def _required_text(form: Mapping[str, str], field_name: str) -> str:
    value = str(form.get(field_name, "")).strip()
    if not value:
        raise WithingsNotificationError(
            400,
            "invalid_request",
            "Invalid Withings notification.",
        )
    return value


def _resource_type_for_category(raw_category: str) -> HealthResourceType:
    try:
        category_id = int(raw_category)
    except ValueError as exc:
        raise WithingsNotificationError(
            400,
            "invalid_request",
            "Invalid Withings notification.",
        ) from exc
    try:
        return _WITHINGS_CATEGORY_MAP[category_id]
    except KeyError as exc:
        raise WithingsNotificationError(
            400,
            "invalid_request",
            "Invalid Withings notification.",
        ) from exc


def _payload_hash(form: Mapping[str, str]) -> str:
    canonical = json.dumps(
        {key: str(value) for key, value in sorted(form.items())},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def ingest_withings_notification(
    pool: object,
    *,
    content_type: str | None,
    form: Mapping[str, str],
) -> WithingsNotificationResult:
    if _normalized_content_type(content_type) != _WITHINGS_WEBHOOK_CONTENT_TYPE:
        raise WithingsNotificationError(
            415,
            "unsupported_media_type",
            "Unsupported notification content type.",
        )

    provider_user_id = _required_text(form, "userid")
    resource_type = _resource_type_for_category(_required_text(form, "appli"))
    payload_hash = _payload_hash(form)

    repository = repository_for(pool)
    async with repository.transaction() as connection:
        mapped_connection = await repository.get_connection_by_provider_user_id(
            provider=HealthProviderSlug.WITHINGS,
            provider_user_id=provider_user_id,
            executor=connection,
        )
        receipt, inserted = await repository.record_webhook_receipt(
            provider=HealthProviderSlug.WITHINGS,
            provider_user_id=provider_user_id,
            resource_type=resource_type,
            payload_hash=payload_hash,
            content_type=content_type,
            status="ignored" if mapped_connection is None else "queued",
            note=None if mapped_connection is not None else "connection_not_found",
            connection_id=None if mapped_connection is None else mapped_connection.connection_id,
            user_id=None if mapped_connection is None else mapped_connection.user_id,
            executor=connection,
        )
        if not inserted:
            return WithingsNotificationResult(
                status="deduplicated",
                resource_type=receipt.resource_type,
            )
        if mapped_connection is None:
            return WithingsNotificationResult(status="ignored", resource_type=resource_type)
        await repository.mark_dirty(
            connection_id=mapped_connection.connection_id,
            user_id=mapped_connection.user_id,
            provider=HealthProviderSlug.WITHINGS,
            resource_type=resource_type,
            reason="webhook",
            source_receipt_id=receipt.receipt_id,
            executor=connection,
        )
        return WithingsNotificationResult(status="queued", resource_type=resource_type)


__all__ = [
    "WithingsNotificationError",
    "WithingsNotificationResult",
    "ingest_withings_notification",
]
