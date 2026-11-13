from __future__ import annotations

import json
from typing import Any

import asyncpg

from nl2sql.schema.profile import DatabaseProfile

# Low-cardinality column threshold: if a column has ≤ this many distinct values,
# include them in the schema context for value grounding.
LOW_CARDINALITY_THRESHOLD = 30

# How many sample rows to show per table
SAMPLE_ROWS = 3


async def get_catalog_hash(pool: asyncpg.Pool) -> str:
    """Hash the current schema to detect changes (for cache invalidation)."""
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            """
            SELECT md5(string_agg(
                table_name || column_name || data_type || COALESCE(column_default, ''),
                '|' ORDER BY table_name, ordinal_position
            ))
            FROM information_schema.columns
            WHERE table_schema = 'public'
            """
        )
    return result or ""


async def introspect_schema(pool: asyncpg.Pool, profile: DatabaseProfile | None = None) -> str:
    """
    Dynamically build a schema context string from pg_catalog:
      1. Tables, columns, types, nullable
      2. PRIMARY KEY and FOREIGN KEY constraints
      3. CHECK constraints (enum values for status, tier, etc.)
      4. Row counts per table
      5. Distinct values for low-cardinality columns (value grounding)
      6. Date min/max for date columns
      7. 3 sample rows per table

    If a profile is given, it supplies the header and receives date ranges
    (keyed "table.column"). Returns a structured string for LLM consumption.
    """
    async with pool.acquire() as conn:
        if profile is None:
            db_name = await conn.fetchval("SELECT current_database()")
            profile = DatabaseProfile.from_db_name(db_name)
        profile.date_ranges.clear()
        tables = await _get_tables(conn)
        output_parts: list[str] = [profile.header()]

        for table in tables:
            table_name = table["table_name"]
            row_count = await _get_row_count(conn, table_name)
            columns = await _get_columns(conn, table_name)
            fks = await _get_foreign_keys(conn, table_name)
            checks = await _get_check_constraints(conn, table_name)
            samples = await _get_sample_rows(conn, table_name)

            output_parts.append(f"--- TABLE: {table_name} ({row_count} rows) ---")

            output_parts.append("  Columns:")
            for col in columns:
                nullable = "nullable" if col["is_nullable"] == "YES" else "not null"
                col_line = f"    - {col['column_name']} ({col['data_type']}, {nullable})"
                output_parts.append(col_line)

                # Distinct values for low-cardinality columns
                if col["data_type"] in ("character varying", "text", "USER-DEFINED"):
                    distinct_vals = await _get_distinct_values(conn, table_name, col["column_name"])
                    if distinct_vals and len(distinct_vals) <= LOW_CARDINALITY_THRESHOLD:
                        output_parts.append(f"      Distinct values: {distinct_vals}")

                # Date range for date columns
                if col["data_type"] == "date":
                    date_range = await _get_date_range(conn, table_name, col["column_name"])
                    if date_range:
                        profile.date_ranges[f"{table_name}.{col['column_name']}"] = date_range
                        output_parts.append(f"      Range: {date_range[0]} to {date_range[1]}")

            if fks:
                output_parts.append("  Foreign Keys:")
                for fk in fks:
                    output_parts.append(
                        f"    - {fk['column_name']} → {fk['foreign_table']}.{fk['foreign_column']}"
                    )

            if checks:
                output_parts.append("  CHECK constraints:")
                for chk in checks:
                    output_parts.append(f"    - {chk}")

            if samples:
                output_parts.append(f"  Sample rows (up to {SAMPLE_ROWS}):")
                for row in samples:
                    output_parts.append(f"    {json.dumps(row, default=str)}")

            output_parts.append("")

    return "\n".join(output_parts)


# ── Helpers ───────────────────────────────────────────────────────────────────


def quote_ident(name: str) -> str:
    """Quote a catalog-derived identifier for safe interpolation into SQL."""
    return '"' + name.replace('"', '""') + '"'


async def _get_tables(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    return [dict(r) for r in rows]


async def _get_row_count(conn: asyncpg.Connection, table: str) -> int:
    # identifier comes from information_schema and is quoted via quote_ident
    sql = f"SELECT COUNT(*) FROM {quote_ident(table)}"  # noqa: S608
    return await conn.fetchval(sql) or 0


async def _get_columns(conn: asyncpg.Connection, table: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        ORDER BY ordinal_position
        """,
        table,
    )
    return [dict(r) for r in rows]


async def _get_foreign_keys(conn: asyncpg.Connection, table: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT
            kcu.column_name,
            ccu.table_name  AS foreign_table,
            ccu.column_name AS foreign_column
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
            ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage AS ccu
            ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND tc.table_name = $1
        """,
        table,
    )
    return [dict(r) for r in rows]


async def _get_check_constraints(conn: asyncpg.Connection, table: str) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT cc.check_clause
        FROM information_schema.table_constraints tc
        JOIN information_schema.check_constraints cc
            ON cc.constraint_name = tc.constraint_name
        WHERE tc.table_schema = 'public'
          AND tc.table_name = $1
          AND tc.constraint_type = 'CHECK'
        """,
        table,
    )
    return [r["check_clause"] for r in rows]


async def _get_distinct_values(
    conn: asyncpg.Connection, table: str, column: str
) -> list[str] | None:
    col, tbl = quote_ident(column), quote_ident(table)
    # identifiers come from information_schema and are quoted via quote_ident
    sql = (
        f"SELECT DISTINCT {col} FROM {tbl} "  # noqa: S608
        f"WHERE {col} IS NOT NULL ORDER BY {col} LIMIT 50"
    )
    rows = await conn.fetch(sql)
    if not rows:
        return None
    vals = [str(r[column]) for r in rows]
    return vals


async def _get_date_range(
    conn: asyncpg.Connection, table: str, column: str
) -> tuple[str, str] | None:
    col = quote_ident(column)
    # identifiers come from information_schema and are quoted via quote_ident
    sql = f"SELECT MIN({col}) AS mn, MAX({col}) AS mx FROM {quote_ident(table)}"  # noqa: S608
    row = await conn.fetchrow(sql)
    if row and row["mn"] and row["mx"]:
        return (str(row["mn"]), str(row["mx"]))
    return None


async def _get_sample_rows(conn: asyncpg.Connection, table: str) -> list[dict[str, Any]]:
    # identifier comes from information_schema; SAMPLE_ROWS is a module constant
    sql = f"SELECT * FROM {quote_ident(table)} LIMIT {int(SAMPLE_ROWS)}"  # noqa: S608
    rows = await conn.fetch(sql)
    return [dict(r) for r in rows]
