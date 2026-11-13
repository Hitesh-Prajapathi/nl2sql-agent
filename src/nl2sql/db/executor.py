from __future__ import annotations

import json
from typing import Any

import asyncpg
from nl2sql.models import ExecutionResult
from nl2sql.config import get_settings

settings = get_settings()
ROW_CAP = settings.query_row_cap

# TODO: move to Settings
MAX_RESULT_BYTES = 5 * 1024 * 1024

_FETCH_BATCH = 1000


def _apply_byte_cap(
    data: list[dict[str, Any]], max_bytes: int
) -> tuple[list[dict[str, Any]], bool]:
    """Keep leading rows whose JSON-serialised size fits in max_bytes."""
    total = 0
    for i, row in enumerate(data):
        total += len(json.dumps(row, default=str))
        if total > max_bytes:
            return data[:i], True
    return data, False


async def execute_sql(
    pool: asyncpg.Pool, sql: str, *, row_cap: int | None = ROW_CAP
) -> ExecutionResult:
    """
    Execute SQL in a read-only transaction using a prepared statement and a
    server-side cursor, so at most row_cap+1 rows are pulled from the server
    (the extra row detects truncation). row_cap=None fetches everything.
    Column names come from the statement, so empty results still have them.
    Results larger than MAX_RESULT_BYTES (JSON-serialised) are truncated.
    Returns ExecutionResult - never raises.
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                stmt = await conn.prepare(sql)
                columns = [a.name for a in stmt.get_attributes()]
                cursor = await stmt.cursor()
                if row_cap is None:
                    raw: list[Any] = []
                    while True:
                        batch = await cursor.fetch(_FETCH_BATCH)
                        if not batch:
                            break
                        raw.extend(batch)
                else:
                    raw = await cursor.fetch(row_cap + 1)

        truncated = row_cap is not None and len(raw) > row_cap
        if truncated:
            raw = raw[:row_cap]
        data = [dict(r) for r in raw]
        data, too_big = _apply_byte_cap(data, MAX_RESULT_BYTES)
        truncated = truncated or too_big

        return ExecutionResult(
            success=True,
            columns=columns,
            rows=data,
            row_count=len(data),
            truncated=truncated,
        )

    except asyncpg.exceptions.ReadOnlySQLTransactionError as e:
        return ExecutionResult(
            success=False,
            error=f"Write operation blocked by read-only transaction: {e}",
            is_safety_block=True,
        )
    except asyncpg.exceptions.QueryCanceledError:
        return ExecutionResult(
            success=False,
            error=(
                "Query was cancelled: statement_timeout exceeded. "
                "Try simplifying the query or narrowing the date range."
            ),
        )
    except asyncpg.exceptions.UndefinedTableError as e:
        return ExecutionResult(success=False, error=f"Table not found: {e}")
    except asyncpg.exceptions.UndefinedColumnError as e:
        return ExecutionResult(success=False, error=f"Column not found: {e}")
    except asyncpg.exceptions.PostgresError as e:
        return ExecutionResult(success=False, error=str(e))
    except Exception as e:
        return ExecutionResult(success=False, error=f"Unexpected error: {e}")
