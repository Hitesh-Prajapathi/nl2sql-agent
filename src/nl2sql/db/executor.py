from __future__ import annotations

import asyncpg
from nl2sql.models import ExecutionResult
from nl2sql.config import get_settings

settings = get_settings()
ROW_CAP = settings.query_row_cap


async def execute_sql(pool: asyncpg.Pool, sql: str) -> ExecutionResult:
    """
    Execute SQL in a read-only transaction.
    Fetches ROW_CAP+1 rows to detect truncation.
    Returns ExecutionResult — never raises.
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                raw = await conn.fetch(sql)

        truncated = len(raw) > ROW_CAP
        rows = raw[: ROW_CAP]
        columns: list[str] = list(rows[0].keys()) if rows else []
        data = [dict(r) for r in rows]

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
