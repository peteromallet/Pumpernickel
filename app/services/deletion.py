"""Deletion grace-period purge job."""

from typing import Any

from app.services.crypto import encrypt_value


async def purge_expired_deletions(pool: Any) -> str:
    return await pool.execute(
        """
        UPDATE messages
        SET content='[deleted]',
            content_encrypted=$1
        WHERE deleted_at IS NOT NULL
          AND deleted_at < now() - interval '24 hours'
          AND content <> '[deleted]'
        """,
        encrypt_value("[deleted]"),
    )
