"""Append-only audit log writer.

Uses its own tiny pool, connecting as ``audit_writer`` (INSERT-only). Every failure
is swallowed and logged: auditing must never fail or slow down a request beyond a
short bounded timeout.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

import asyncpg
import structlog

from nl2sql.config import get_settings

log = structlog.get_logger()

_RETRY_AFTER_FAILURE_S = 30.0
_WRITE_TIMEOUT_S = 2.0


def hash_question(question: str) -> str:
    return hashlib.sha256(question.strip().encode()).hexdigest()


@dataclass
class AuditRecord:
    request_id: str
    key_id: str | None
    question_hash: str
    sql_text: str | None
    status: str
    blocked_layer: str | None
    row_count: int | None
    latency_ms: float
    cost_usd: float | None
    trace_id: str | None


class AuditWriter:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._down_until = 0.0
        self._lock = asyncio.Lock()
        self.failures = 0

    async def _get_pool(self) -> asyncpg.Pool | None:
        s = get_settings()
        if not s.audit_enabled or time.monotonic() < self._down_until:
            return None
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(
                        s.audit_database_url,
                        min_size=1,
                        max_size=max(1, s.audit_pool_max_size),
                        command_timeout=_WRITE_TIMEOUT_S,
                        timeout=_WRITE_TIMEOUT_S,
                    )
        return self._pool

    async def write(self, rec: AuditRecord) -> bool:
        """Best-effort insert. Returns True if the row was written; never raises."""
        try:
            pool = await asyncio.wait_for(self._get_pool(), timeout=_WRITE_TIMEOUT_S + 1)
            if pool is None:
                return False
            await pool.execute(
                "INSERT INTO audit.query_log (request_id, key_id, question_hash, sql_text,"
                " status, blocked_layer, row_count, latency_ms, cost_usd, trace_id)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                rec.request_id,
                rec.key_id,
                rec.question_hash,
                rec.sql_text,
                rec.status,
                rec.blocked_layer,
                rec.row_count,
                rec.latency_ms,
                rec.cost_usd,
                rec.trace_id,
            )
            return True
        except Exception as exc:
            self.failures += 1
            self._down_until = time.monotonic() + _RETRY_AFTER_FAILURE_S
            log.warning("audit_write_failed", error_type=type(exc).__name__)
            return False

    async def spent_today_usd(self) -> float | None:
        """Sum of today's cost across all workers (None if unavailable)."""
        try:
            pool = await asyncio.wait_for(self._get_pool(), timeout=_WRITE_TIMEOUT_S + 1)
            if pool is None:
                return None
            val = await pool.fetchval(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM audit.query_log"
                " WHERE ts >= date_trunc('day', now())"
            )
            return float(val)
        except Exception:
            return None

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
