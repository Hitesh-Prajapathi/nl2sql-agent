from __future__ import annotations

import asyncpg

from nl2sql.config import get_settings

settings = get_settings()

_pool: asyncpg.Pool | None = None


async def create_pool(
    dsn: str,
    *,
    min_size: int = 2,
    max_size: int = 10,
    statement_timeout_ms: int | None = None,
    search_path: str | None = None,
) -> asyncpg.Pool:
    """Create a read-only pool. ``search_path`` pins the schema for schema-per-database layouts."""
    server_settings = {
        # Defence-in-depth: enforced at the connection level too.
        # The DB role is the real guarantee; this is an additional session-level setting.
        "default_transaction_read_only": "on",
        "statement_timeout": str(statement_timeout_ms or settings.statement_timeout_ms),
        "lock_timeout": str(settings.lock_timeout_ms),
        "idle_in_transaction_session_timeout": "30000",
    }
    if search_path:
        server_settings["search_path"] = search_path
    return await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=max(30.0, (statement_timeout_ms or 0) / 1000 + 5),
        server_settings=server_settings,
    )


async def get_pool() -> asyncpg.Pool:
    """Return the global read-only connection pool, initialising it if needed."""
    global _pool
    if _pool is None:
        _pool = await create_pool(settings.database_url)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
