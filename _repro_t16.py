"""Reproduce the T16 bug: correct_reflection references an undefined helper."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.services.tools import reflection_tools as rt


async def main():
    # _store(ctx) returns ReflectionStore(ctx.pool). Patch to a fake store whose
    # correct_entry returns a fake entry, so we reach the reconciliation call.
    fake_entry = MagicMock()
    fake_entry.id = uuid4()
    fake_entry.session_id = uuid4()
    fake_entry.supersedes_entry_id = uuid4()
    fake_entry.revision_number = 2
    fake_entry.created_at = "2026-07-19T00:00:00Z"

    fake_store = MagicMock()
    fake_store.correct_entry = AsyncMock(return_value=fake_entry)

    fake_ctx = MagicMock()
    fake_ctx.user_id = uuid4()
    fake_ctx.bot_id = "bot-1"
    fake_ctx.pool = None

    from tool_schemas import CorrectReflectionInput
    args = CorrectReflectionInput(
        supersedes_entry_id=uuid4(),
        correction_note="fixing the reflection",
    )

    orig_store = rt._store
    rt._store = lambda ctx: fake_store
    try:
        out = await rt.correct_reflection(fake_ctx, args)
    finally:
        rt._store = orig_store
    print("RESULT:", out.model_dump() if hasattr(out, "model_dump") else out)


if __name__ == "__main__":
    asyncio.run(main())
