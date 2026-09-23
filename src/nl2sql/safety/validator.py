from __future__ import annotations

import re
from typing import Set

import sqlglot
from sqlglot import exp

from nl2sql.models import SQLValidationResult

# ── Dangerous PG functions that must never be called ─────────────────────────
DANGEROUS_FUNCTIONS: Set[str] = {
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "lo_get",
    "lo_put",
    "lo_create",
    "lo_unlink",
    "dblink",
    "dblink_exec",
    "dblink_connect",
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "copy_file_range",
}

# ── System schemas that must never be accessed ────────────────────────────────
BLOCKED_SCHEMAS: Set[str] = {
    "pg_catalog",
    "information_schema",
    "pg_toast",
    "pg_temp",
}

# ── Statement types that are always terminal refusals ─────────────────────────
BLOCKED_STATEMENT_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.AlterColumn,
    exp.Grant,
    exp.Revoke,
    exp.Command,      # TRUNCATE, VACUUM, etc.
    exp.Transaction,  # BEGIN, COMMIT, ROLLBACK
)


def validate_sql(sql: str) -> SQLValidationResult:
    """
    Full AST-based SQL validation using sqlglot (Postgres dialect).

    Rules enforced:
      1. Exactly one statement
      2. Root node is SELECT (or WITH whose body resolves to SELECT)
      3. No DML/DDL anywhere in the AST, including inside CTEs
      4. No SELECT ... INTO or FOR UPDATE/SHARE
      5. No dangerous functions
      6. No access to system schemas (pg_catalog, information_schema, etc.)

    Returns SQLValidationResult:
      - is_valid=True  → proceed to execution
      - is_blocked=True → terminal refusal (do NOT retry)
      - is_valid=False, is_blocked=False → retryable syntax error
    """
    sql = sql.strip()
    if not sql:
        return SQLValidationResult(is_valid=False, error="Empty SQL string.")

    # ── Parse ────────────────────────────────────────────────────────────────
    try:
        statements = sqlglot.parse(sql, dialect="postgres", error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError as exc:
        return SQLValidationResult(is_valid=False, error=f"SQL syntax error: {exc}")

    # ── Rule 1: Exactly one statement ────────────────────────────────────────
    if len(statements) != 1:
        return SQLValidationResult(
            is_valid=False,
            is_blocked=True,
            error=f"Multiple statements are not allowed. Expected exactly 1 statement, got {len(statements)}. Stacked statements are not allowed.",
        )

    stmt = statements[0]

    # ── Rule 2: Root must be SELECT (or WITH body = SELECT) ──────────────────
    root_ok = isinstance(stmt, (exp.Select, exp.Union, exp.Intersect, exp.Except))
    if not root_ok:
        if isinstance(stmt, exp.With):
            # CTE: body must resolve to a SELECT
            if not stmt.find(exp.Select):
                return SQLValidationResult(
                    is_valid=False,
                    is_blocked=True,
                    error="CTE body does not contain a SELECT statement.",
                )
        else:
            return SQLValidationResult(
                is_valid=False,
                is_blocked=True,
                error=f"Only SELECT statements are allowed. Got: {type(stmt).__name__}",
            )

    # ── Rule 3: No DML/DDL anywhere in the tree (including inside CTEs) ──────
    for node in stmt.walk():
        if isinstance(node, BLOCKED_STATEMENT_TYPES):
            return SQLValidationResult(
                is_valid=False,
                is_blocked=True,
                error=f"Blocked operation found in query tree: {type(node).__name__}",
            )

    # ── Rule 4: No SELECT ... INTO or FOR UPDATE/SHARE ───────────────────────
    if stmt.find(exp.Into):
        return SQLValidationResult(
            is_valid=False,
            is_blocked=True,
            error="SELECT ... INTO is not allowed.",
        )
    if stmt.find(exp.Lock):
        return SQLValidationResult(
            is_valid=False,
            is_blocked=True,
            error="FOR UPDATE / FOR SHARE is not allowed.",
        )

    # ── Rule 5: No dangerous functions ───────────────────────────────────────
    for func in stmt.find_all(exp.Anonymous):
        name = func.name.lower() if func.name else ""
        if name in DANGEROUS_FUNCTIONS:
            return SQLValidationResult(
                is_valid=False,
                is_blocked=True,
                error=f"Dangerous function blocked: {name}()",
            )
    for func in stmt.find_all(exp.Func):
        name = getattr(func, "sql_name", lambda: "")().lower()
        if name in DANGEROUS_FUNCTIONS:
            return SQLValidationResult(
                is_valid=False,
                is_blocked=True,
                error=f"Dangerous function blocked: {name}()",
            )

    # ── Rule 6: No access to blocked schemas / system tables ─────────────────
    for table in stmt.find_all(exp.Table):
        db = (table.db or "").lower()
        name = (table.name or "").lower()
        if db in BLOCKED_SCHEMAS or name.startswith("pg_") or name.startswith("information_schema"):
            return SQLValidationResult(
                is_valid=False,
                is_blocked=True,
                error=f"Access to system schema/table blocked: {db + '.' if db else ''}{name}",
            )

    return SQLValidationResult(is_valid=True)


# ── Prompt-injection detection (input guard) ─────────────────────────────────
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(previous|above|all|prior)\s+(instructions?|prompts?|context)", re.I),
    re.compile(r"you\s+are\s+now\s+", re.I),
    re.compile(r"(system|assistant)\s*:\s*", re.I),
    re.compile(r"forget\s+(everything|all|your|what)", re.I),
    re.compile(r"new\s+(role|persona|instructions?)", re.I),
    re.compile(r"jailbreak", re.I),
    re.compile(r"DAN\s+mode", re.I),
]


def detect_prompt_injection(text: str) -> bool:
    """Return True if the text looks like a prompt-injection attempt."""
    return any(p.search(text) for p in INJECTION_PATTERNS)
