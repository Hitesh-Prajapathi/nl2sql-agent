"""Schema introspection and profiling that scales.

Everything is read from ``pg_catalog`` for the connection's the resolved schema (so a pool whose
role pins ``search_path`` to one schema introspects exactly that database):

* row counts come from ``pg_class.reltuples`` (an exact ``COUNT(*)`` is used only when the table
  was never analyzed, ``reltuples < 0``, and is small enough to be cheap);
* value grounding comes from ``pg_stats`` (``n_distinct``, ``most_common_vals``) instead of one
  ``SELECT DISTINCT`` per text column;
* the output is a plain-dict profile (JSON-serialisable, buildable offline and versioned) that is
  rendered to prompt text under a per-table token budget with truncated sample values.
"""

from __future__ import annotations

import json
import re
from typing import Any

import asyncpg

from nl2sql.schema.profile import DatabaseProfile

PROFILE_FORMAT_VERSION = 1

# A string column with <= this many distinct values is shown as a complete value list.
LOW_CARDINALITY_THRESHOLD = 30
# How many sample rows to show per table (before budget degradation).
SAMPLE_ROWS = 3
# Max non-exhaustive common values listed per column.
MAX_COMMON_VALUES = 8
# Max characters of any single sample / common value shown to the model.
MAX_VALUE_CHARS = 60
# Default per-table budget in (approximate) tokens for the rendered table block.
DEFAULT_TABLE_TOKEN_BUDGET = 1500
# Exact MIN/MAX for date columns only on tables up to this many rows; histogram bounds beyond.
EXACT_RANGE_MAX_ROWS = 1_000_000
# Exact COUNT(*) fallback only for never-analyzed tables up to this size (unknown size -> try).
_NEVER_ANALYZED = -1

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~4 characters per token)."""
    return (len(text) + 3) // 4


def sanitize_value(value: Any, max_chars: int = MAX_VALUE_CHARS) -> str:
    """Render a data value for a prompt: single line, control chars removed, truncated."""
    text = _CTRL.sub(" ", str(value)).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text


def sample_value(value: Any) -> Any:
    """Sample-row cell: numbers/bools/None stay native JSON, everything else is a clean string."""
    if value is None or isinstance(value, bool | int | float):
        return value
    return sanitize_value(value)


def quote_ident(name: str) -> str:
    """Quote a catalog-derived identifier for safe interpolation into SQL."""
    return '"' + name.replace('"', '""') + '"'


# ── Schema resolution ─────────────────────────────────────────────────────────


async def resolve_schema(conn: asyncpg.Connection) -> str:
    """The schema unqualified names resolve to: first existing entry of ``search_path``.

    Uses ``SHOW`` rather than the resolved schema: hardened read-only roles may lack EXECUTE on
    that function.
    """
    path = str(await conn.fetchval("SHOW search_path"))
    user = str(await conn.fetchval("SELECT session_user"))
    for raw in path.split(","):
        name = raw.strip().strip('"').replace('""', '"')
        if name == "$user":
            name = user
        exists = await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", name)
        if exists:
            return name
    return "public"


# ── Catalog hash ──────────────────────────────────────────────────────────────


async def get_catalog_hash(pool: asyncpg.Pool) -> str:
    """Hash the current schema to detect changes (for cache invalidation)."""
    async with pool.acquire() as conn:
        schema = await resolve_schema(conn)
        result = await conn.fetchval(
            """
            SELECT md5(string_agg(
                table_name || column_name || data_type || COALESCE(column_default, ''),
                '|' ORDER BY table_name, ordinal_position
            ))
            FROM information_schema.columns
            WHERE table_schema = $1
            """,
            schema,
        )
    return str(result or "")


# ── Building the profile (database access) ────────────────────────────────────


async def build_profile_data(
    pool: asyncpg.Pool, profile: DatabaseProfile | None = None
) -> dict[str, Any]:
    """Collect a JSON-serialisable profile of the resolved schema.

    If a ``DatabaseProfile`` is passed its ``date_ranges`` is refilled (keyed ``table.column``).
    """
    async with pool.acquire() as conn:
        schema = await resolve_schema(conn)
        if profile is None:
            db_name = await conn.fetchval("SELECT current_database()")
            profile = DatabaseProfile.from_db_name(schema if schema != "public" else str(db_name))
        profile.date_ranges.clear()
        tables: list[dict[str, Any]] = []
        for trow in await _get_tables(conn, schema):
            tables.append(await _profile_table(conn, schema, trow, profile))
    return {
        "format_version": PROFILE_FORMAT_VERSION,
        "db_id": profile.db_id,
        "display_name": profile.display_name,
        "domain_description": profile.domain_description,
        "schema": schema,
        "tables": tables,
        "date_ranges": {k: list(v) for k, v in profile.date_ranges.items()},
    }


async def _profile_table(
    conn: asyncpg.Connection, schema: str, trow: dict[str, Any], profile: DatabaseProfile
) -> dict[str, Any]:
    name = trow["name"]
    oid = trow["oid"]
    reltuples = float(trow["reltuples"])
    if reltuples == _NEVER_ANALYZED:
        count = await conn.fetchval(
            f"SELECT COUNT(*) FROM {quote_ident(schema)}.{quote_ident(name)}"  # noqa: S608
        )
        rows, estimated = int(count or 0), False
    else:
        rows, estimated = int(reltuples), True

    cols = await _get_columns(conn, oid)
    stats = await _get_stats(conn, schema, name)
    columns: list[dict[str, Any]] = []
    for col in cols:
        st = stats.get(col["name"], {})
        entry: dict[str, Any] = {
            "name": col["name"],
            "type": col["type"],
            "nullable": col["nullable"],
        }
        n_distinct = _absolute_n_distinct(st.get("n_distinct"), rows)
        if n_distinct is not None:
            entry["n_distinct"] = n_distinct
        if st.get("null_frac"):
            entry["null_frac"] = round(float(st["null_frac"]), 3)
        values: list[str] | None = None
        if col["is_stringy"]:
            values = st.get("common_vals")
            if values is None and rows <= 100_000 and not st:
                values = await _distinct_fallback(conn, schema, name, col["name"])
            if values:
                complete = (
                    n_distinct is not None
                    and n_distinct <= LOW_CARDINALITY_THRESHOLD
                    and len(values) >= n_distinct
                ) or (n_distinct is None and len(values) <= LOW_CARDINALITY_THRESHOLD and not st)
                entry["values"] = [sanitize_value(v) for v in values[:50]]
                entry["values_complete"] = bool(complete)
        if col["is_date"]:
            rng = await _date_range(conn, schema, name, col["name"], rows, st)
            if rng:
                entry["range"] = list(rng)
                profile.date_ranges[f"{name}.{col['name']}"] = rng
        columns.append(entry)

    samples = await conn.fetch(
        f"SELECT * FROM {quote_ident(schema)}.{quote_ident(name)} LIMIT {int(SAMPLE_ROWS)}"  # noqa: S608
    )
    return {
        "name": name,
        "rows": rows,
        "rows_estimated": estimated,
        "columns": columns,
        "primary_key": await _get_primary_key(conn, oid),
        "foreign_keys": await _get_foreign_keys(conn, oid),
        "checks": await _get_check_constraints(conn, oid),
        "samples": [{k: sample_value(v) for k, v in dict(r).items()} for r in samples],
    }


def _absolute_n_distinct(n_distinct: float | None, rows: int) -> int | None:
    """pg_stats.n_distinct: >0 is a count, <0 is -(fraction of rows)."""
    if n_distinct is None:
        return None
    if n_distinct < 0:
        return max(1, round(-n_distinct * max(rows, 1)))
    return int(n_distinct)


# ── Rendering (pure, offline) ─────────────────────────────────────────────────


def render_context(
    data: dict[str, Any],
    table_token_budget: int = DEFAULT_TABLE_TOKEN_BUDGET,
    profile: DatabaseProfile | None = None,
) -> str:
    """Render a profile dict as prompt text, degrading each table to fit its token budget."""
    if profile is None:
        profile = DatabaseProfile(
            db_id=data["db_id"],
            display_name=data.get("display_name") or data["db_id"],
            domain_description=data.get("domain_description"),
        )
    parts: list[str] = [profile.header()]
    for table in data["tables"]:
        parts.append(_render_table_within_budget(table, table_token_budget))
        parts.append("")
    return "\n".join(parts)


def _render_table_within_budget(table: dict[str, Any], budget: int) -> str:
    # progressively cheaper renderings: (max_values, n_samples, show_ranges)
    levels = [(30, SAMPLE_ROWS, True), (12, 2, True), (6, 1, True), (3, 0, True), (0, 0, False)]
    text = ""
    for max_values, n_samples, show_ranges in levels:
        text = render_table(table, max_values, n_samples, show_ranges)
        if estimate_tokens(text) <= budget:
            return text
    return text  # column list alone is over budget: keep it, a truncated schema is worse


def render_table(
    table: dict[str, Any], max_values: int = 30, n_samples: int = SAMPLE_ROWS, ranges: bool = True
) -> str:
    approx = "~" if table.get("rows_estimated") else ""
    out = [f"--- TABLE: {table['name']} ({approx}{table['rows']} rows) ---", "  Columns:"]
    for col in table["columns"]:
        nullable = "nullable" if col["nullable"] else "not null"
        line = f"    - {col['name']} ({col['type']}, {nullable})"
        out.append(line)
        values = col.get("values")
        if values and max_values > 0:
            complete = bool(col.get("values_complete"))
            # Non-exhaustive "common values" are only a hint: cap them harder than full lists.
            shown = values[: max_values if complete else min(max_values, MAX_COMMON_VALUES)]
            if complete and len(shown) == len(values):
                label, more = "Distinct values", ""
            else:
                label, more = "Common values", f" (of ~{col.get('n_distinct', '?')})"
            out.append(f"      {label}{more}: {shown}")
        if ranges and col.get("range"):
            out.append(f"      Range: {col['range'][0]} to {col['range'][1]}")
    if table.get("primary_key"):
        out.append(f"  Primary Key: {', '.join(table['primary_key'])}")
    if table.get("foreign_keys"):
        out.append("  Foreign Keys:")
        for fk in table["foreign_keys"]:
            out.append(f"    - {fk['column']} → {fk['foreign_table']}.{fk['foreign_column']}")
    if table.get("checks"):
        out.append("  CHECK constraints:")
        out.extend(f"    - {chk}" for chk in table["checks"])
    samples = table.get("samples", [])[:n_samples]
    if samples:
        out.append(f"  Sample rows (up to {len(samples)}):")
        out.extend(f"    {json.dumps(s, ensure_ascii=False)}" for s in samples)
    return "\n".join(out)


# ── Convenience: build + render in one go (kept for existing callers/tests) ──


async def introspect_schema(
    pool: asyncpg.Pool,
    profile: DatabaseProfile | None = None,
    table_token_budget: int = DEFAULT_TABLE_TOKEN_BUDGET,
) -> str:
    """Build a schema context string from pg_catalog / pg_stats (see module docstring)."""
    if profile is None:
        # Resolved inside build_profile_data so one pool acquire is enough.
        data = await build_profile_data(pool, None)
        profile = DatabaseProfile(
            db_id=data["db_id"],
            display_name=data["display_name"],
            domain_description=data.get("domain_description"),
            date_ranges={k: (v[0], v[1]) for k, v in data["date_ranges"].items()},
        )
    else:
        data = await build_profile_data(pool, profile)
    return render_context(data, table_token_budget, profile)


# ── Catalog queries ───────────────────────────────────────────────────────────


async def _get_tables(conn: asyncpg.Connection, schema: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT c.oid, c.relname AS name, c.reltuples
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = $1 AND c.relkind IN ('r', 'p')
        ORDER BY c.relname
        """,
        schema,
    )
    return [dict(r) for r in rows]


async def _get_columns(conn: asyncpg.Connection, table_oid: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT a.attname AS name,
               format_type(a.atttypid, a.atttypmod) AS type,
               NOT a.attnotnull AS nullable,
               (t.typcategory = 'S' OR t.typtype = 'e') AS is_stringy,
               (t.typname IN ('date', 'timestamp', 'timestamptz')) AS is_date
        FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
        WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        table_oid,
    )
    return [dict(r) for r in rows]


async def _get_stats(
    conn: asyncpg.Connection, schema: str, table: str
) -> dict[str, dict[str, Any]]:
    try:
        rows = await conn.fetch(
            """
            SELECT attname, null_frac, n_distinct,
                   most_common_vals::text::text[] AS mcv,
                   histogram_bounds::text::text[] AS hist
            FROM pg_stats WHERE schemaname = $1 AND tablename = $2
            """,
            schema,
            table,
        )
    except asyncpg.exceptions.InsufficientPrivilegeError:
        return {}  # hardened role without pg_stats access: fall back to bounded queries
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r["attname"]] = {
            "null_frac": r["null_frac"],
            "n_distinct": r["n_distinct"],
            "common_vals": list(r["mcv"]) if r["mcv"] else None,
            "hist": list(r["hist"]) if r["hist"] else None,
        }
    return out


async def _distinct_fallback(
    conn: asyncpg.Connection, schema: str, table: str, column: str
) -> list[str] | None:
    """Only for small never-analyzed tables (no pg_stats rows)."""
    col, tbl = quote_ident(column), f"{quote_ident(schema)}.{quote_ident(table)}"
    sql = (
        f"SELECT DISTINCT {col}::text AS v FROM {tbl} "  # noqa: S608
        f"WHERE {col} IS NOT NULL ORDER BY 1 LIMIT {LOW_CARDINALITY_THRESHOLD + 1}"
    )
    rows = await conn.fetch(sql)
    return [r["v"] for r in rows] or None


async def _date_range(
    conn: asyncpg.Connection,
    schema: str,
    table: str,
    column: str,
    rows: int,
    stats: dict[str, Any],
) -> tuple[str, str] | None:
    hist = stats.get("hist")
    if rows > EXACT_RANGE_MAX_ROWS and hist:
        return (str(hist[0]), str(hist[-1]))
    col = quote_ident(column)
    sql = (
        f"SELECT MIN({col}) AS mn, MAX({col}) AS mx "  # noqa: S608
        f"FROM {quote_ident(schema)}.{quote_ident(table)}"
    )
    row = await conn.fetchrow(sql)
    if row and row["mn"] and row["mx"]:
        return (str(row["mn"]), str(row["mx"]))
    return None


async def _get_primary_key(conn: asyncpg.Connection, table_oid: int) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT a.attname FROM pg_constraint c
        JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.conrelid = $1 AND c.contype = 'p' ORDER BY k.ord
        """,
        table_oid,
    )
    return [r["attname"] for r in rows]


async def _get_foreign_keys(conn: asyncpg.Connection, table_oid: int) -> list[dict[str, str]]:
    rows = await conn.fetch(
        """
        SELECT a.attname AS col, ft.relname AS ftable, fa.attname AS fcol
        FROM pg_constraint c
        JOIN pg_class ft ON ft.oid = c.confrelid
        JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(attnum, fattnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = k.fattnum
        WHERE c.conrelid = $1 AND c.contype = 'f' ORDER BY c.conname, k.ord
        """,
        table_oid,
    )
    return [
        {"column": r["col"], "foreign_table": r["ftable"], "foreign_column": r["fcol"]}
        for r in rows
    ]


async def _get_check_constraints(conn: asyncpg.Connection, table_oid: int) -> list[str]:
    rows = await conn.fetch(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conrelid = $1 AND contype = 'c' ORDER BY conname",
        table_oid,
    )
    return [sanitize_value(r["d"], 300) for r in rows]
