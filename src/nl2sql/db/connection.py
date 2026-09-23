from __future__ import annotations

import asyncpg

from nl2sql.config import get_settings

settings = get_settings()

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the global read-only connection pool, initialising it if needed."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=2,
            max_size=10,
            command_timeout=30,
            server_settings={
                # Defence-in-depth: enforced at the connection level too.
                # Layer 1 (DB role) is the real guarantee; this is Layer 3.
                "default_transaction_read_only": "on",
                "statement_timeout": str(settings.statement_timeout_ms),
                "lock_timeout": str(settings.lock_timeout_ms),
                "idle_in_transaction_session_timeout": "30000",
            },
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
