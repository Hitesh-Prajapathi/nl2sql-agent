"""Evaluation-owned SQL execution: positional tuples, no row or byte caps, timed.

Scoring must not depend on ``exec_rows`` from the agent: ``db.executor`` keys rows by column name
(duplicate output names such as ``SELECT T1.id, T2.id`` collapse) and truncates at 5 MB. Both the
prediction and the gold SQL are therefore re-executed here. Gold SQL deliberately bypasses
``validate_sql`` (it is trusted benchmark data, and the number of gold queries the validator would
block is reported separately by ``nl2sql-eval check-gold``).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg

from nl2sql.db.registry import DatabaseRegistry
from nl2sql.eval.metrics import Row, normalize_rows


@dataclass
class SqlOutcome:
    rows: list[Row] | None
    error: str | None
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return self.error is None and self.rows is not None


class SqlRunner(Protocol):
    async def run(self, db_id: str, sql: str, timeout_s: float = 30.0) -> SqlOutcome: ...


class RegistrySqlRunner:
    """Executes SQL through the per-database read-only pools of a ``DatabaseRegistry``."""

    def __init__(self, registry: DatabaseRegistry) -> None:
        self._registry = registry

    async def run(self, db_id: str, sql: str, timeout_s: float = 30.0) -> SqlOutcome:
        pool = await self._registry.pool_for(db_id)
        t0 = time.perf_counter()
        try:
            async with pool.acquire() as conn, conn.transaction(readonly=True):
                t0 = time.perf_counter()  # time execute + fetch only, not acquire
                records = await asyncio.wait_for(conn.fetch(sql), timeout=timeout_s)
                elapsed = time.perf_counter() - t0
            rows = normalize_rows([tuple(r.values()) for r in records])
            return SqlOutcome(rows=rows, error=None, elapsed_s=elapsed)
        except TimeoutError:
            return SqlOutcome(None, f"timeout after {timeout_s:.0f}s", time.perf_counter() - t0)
        except asyncpg.PostgresError as exc:
            return SqlOutcome(None, f"{type(exc).__name__}: {exc}", time.perf_counter() - t0)
        except Exception as exc:
            return SqlOutcome(None, f"{type(exc).__name__}: {exc}", time.perf_counter() - t0)


def result_digest(rows: list[Row]) -> dict[str, Any]:
    """Compact fingerprint of a result set (for gold checks without storing the rows)."""
    import hashlib

    h = hashlib.sha256()
    for r in sorted(map(repr, set(rows))):
        h.update(r.encode())
    return {"n_rows": len(rows), "sha256": h.hexdigest()}
