"""LLM spend-cap helpers."""

from decimal import Decimal
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def _cap_for(provider: str) -> Decimal:
    settings = get_settings()
    caps = {
        "text": settings.text_llm_daily_cap_usd,
        "vision": settings.vision_daily_cap_usd,
        "transcription": settings.transcription_daily_cap_usd,
    }
    if provider not in caps:
        raise ValueError(f"Unknown LLM spend provider: {provider}")
    return Decimal(str(caps[provider]))


async def record_llm_cost(pool: Any, provider: str, dollars: float | Decimal) -> None:
    await pool.execute(
        """
        INSERT INTO llm_spend_log (provider, day, total_usd)
        VALUES ($1, CURRENT_DATE, $2)
        ON CONFLICT (provider, day)
        DO UPDATE SET
            total_usd = llm_spend_log.total_usd + EXCLUDED.total_usd,
            updated_at = now()
        """,
        provider,
        Decimal(str(dollars)),
    )
    total = Decimal(
        str(
            await pool.fetchval(
                """
                SELECT total_usd
                FROM llm_spend_log
                WHERE provider = $1
                  AND day = CURRENT_DATE
                """,
                provider,
            )
            or 0
        )
    )
    cap = _cap_for(provider)
    if cap > 0 and total >= cap * Decimal("0.80"):
        warned_at = await pool.fetchval(
            """
            SELECT warned_80_at
            FROM llm_spend_log
            WHERE provider = $1
              AND day = CURRENT_DATE
            """,
            provider,
        )
        if warned_at is not None:
            return
        await pool.execute(
            """
            UPDATE llm_spend_log
            SET warned_80_at = COALESCE(warned_80_at, now())
            WHERE provider = $1
              AND day = CURRENT_DATE
              AND warned_80_at IS NULL
            """,
            provider,
        )
        logger.warning("LLM spend for provider=%s crossed 80%% of daily cap: total=%s cap=%s", provider, total, cap)


async def is_under_cap(pool: Any, provider: str) -> bool:
    _cap_for(provider)
    return True


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage_tokens(usage: Any, field: str) -> int:
    return int(_attr(usage, field, 0) or 0)


async def record_anthropic_haiku_text_response_cost(pool: Any, usage: Any) -> None:
    settings = get_settings()
    input_tokens = _usage_tokens(usage, "input_tokens")
    cache_create = _usage_tokens(usage, "cache_creation_input_tokens")
    cache_read = _usage_tokens(usage, "cache_read_input_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens")
    regular_input_tokens = max(0, input_tokens - cache_create - cache_read)
    input_rate = Decimal(str(settings.anthropic_haiku_input_usd_per_mtok))
    output_rate = Decimal(str(settings.anthropic_haiku_output_usd_per_mtok))
    dollars = (
        regular_input_tokens * input_rate
        + cache_create * input_rate * Decimal("1.25")
        + cache_read * input_rate * Decimal("0.10")
        + output_tokens * output_rate
    ) / Decimal("1000000")
    if dollars > 0:
        await record_llm_cost(pool, "text", dollars)
