"""Postgres-backed SessionStore (table ``app_state.sessions``, role ``app_state_writer``).

Appends run in one transaction holding a row lock (``SELECT ... FOR UPDATE``), so
concurrent appends to the same thread serialise and none is lost. Expiry uses the
database clock (``now()``), which is consistent across workers. Unlike the in-memory
store there is no ``max_sessions`` cap; the TTL bounds growth (expired rows are
purged opportunistically).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg

from nl2sql.api.sessions import trim_turns

_ENSURE_ROW = """
INSERT INTO app_state.sessions (session_id, turns, expires_at)
VALUES ($1, '[]'::jsonb, now())
ON CONFLICT (session_id) DO NOTHING
"""
_LOCK_ROW = """
SELECT turns::text AS turns, expires_at > now() AS live
FROM app_state.sessions WHERE session_id = $1 FOR UPDATE
"""
_UPDATE = """
UPDATE app_state.sessions
SET turns = $2::jsonb, expires_at = now() + make_interval(secs => $3), updated_at = now()
WHERE session_id = $1
"""
_GET = """
SELECT turns::text FROM app_state.sessions WHERE session_id = $1 AND expires_at > now()
"""
_PURGE = """
DELETE FROM app_state.sessions
WHERE session_id IN (
    SELECT session_id FROM app_state.sessions
    WHERE expires_at < now() - interval '1 minute' LIMIT 50 FOR UPDATE SKIP LOCKED
)
"""


class PostgresSessionStore:
    def __init__(
        self,
        dsn: str,
        ttl_seconds: float,
        max_turns: int,
        max_bytes: int,
        pool_max_size: int = 3,
    ) -> None:
        self.dsn = dsn
        self.ttl = float(ttl_seconds)
        self.max_turns = max_turns
        self.max_bytes = max_bytes
        self._pool_max = pool_max_size
        self._pool: asyncpg.Pool | None = None
        self._pool_loop: asyncio.AbstractEventLoop | None = None

    async def _get_pool(self) -> asyncpg.Pool:
        loop = asyncio.get_running_loop()
        if self._pool is None or self._pool_loop is not loop:
            # Two first-use callers may both create a pool; keep one, close the extra.
            pool = await asyncpg.create_pool(
                self.dsn, min_size=1, max_size=self._pool_max, command_timeout=5
            )
            if self._pool is not None and self._pool_loop is loop:
                await pool.close()
                return self._pool
            self._pool, self._pool_loop = pool, loop
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def get(self, session_id: str) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        raw = await pool.fetchval(_GET, session_id)
        return list(json.loads(raw)) if raw else []

    async def append(self, session_id: str, turn: dict[str, Any]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(_ENSURE_ROW, session_id)
            row = await conn.fetchrow(_LOCK_ROW, session_id)
            existing: list[dict[str, Any]] = (
                json.loads(row["turns"]) if row is not None and row["live"] else []
            )
            turns = trim_turns([*existing, turn], self.max_turns, self.max_bytes)
            await conn.execute(_UPDATE, session_id, json.dumps(turns, default=str), self.ttl)
            await conn.execute(_PURGE)
