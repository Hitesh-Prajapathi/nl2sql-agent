"""Raw ``CREATE TABLE`` DDL from a stored profile (the naive baseline's schema), optionally capped.

Deliberately no sample rows, value lists or ranges: that is what the naive baseline lacks.
"""

from __future__ import annotations

from typing import Any

from nl2sql.llm.tokens import estimate_text_tokens
from nl2sql.schema.capped import kept_columns, rank_tables


def table_ddl(table: dict[str, Any], columns: list[dict[str, Any]] | None = None) -> str:
    cols = table["columns"] if columns is None else columns
    kept = {c["name"] for c in cols}
    lines = [f'  "{c["name"]}" {c["type"]}{"" if c["nullable"] else " NOT NULL"}' for c in cols]
    pk = [c for c in table.get("primary_key") or [] if c in kept]
    if pk:
        lines.append("  PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk) + ")")
    for fk in table.get("foreign_keys", []):
        if fk["column"] in kept:
            ref = f'"{fk["foreign_table"]}"("{fk["foreign_column"]}")'
            lines.append(f'  FOREIGN KEY ("{fk["column"]}") REFERENCES {ref}')
    return f'CREATE TABLE "{table["name"]}" (\n' + ",\n".join(lines) + "\n);"


def build_ddl(profile: dict[str, Any]) -> str:
    """Raw CREATE TABLE statements (columns, types, PK, FK) from a stored catalog profile."""
    return "\n\n".join(table_ddl(t) for t in profile["tables"])


def build_ddl_capped(
    profile: dict[str, Any], question: str, evidence: str, budget_tokens: int
) -> str:
    """DDL of the tables most relevant to the question, within ``budget_tokens`` (estimated).

    Tables are ranked like ``schema.capped`` (lexical overlap + FK neighbours). A table that does
    not fit whole is cut down to its matched, primary key and foreign key columns.
    """
    by_name = {t["name"]: t for t in profile["tables"]}
    parts: list[str] = []
    used = 0
    for rank in rank_tables(profile, question, evidence):
        table = by_name[rank.name]
        remaining = budget_tokens - used - 2
        if remaining <= 0:
            break
        candidates = [table_ddl(table)]
        for max_cols in (None, 10, 5):
            cols = kept_columns(table, rank.matched_columns, max_cols)
            if len(cols) < len(table["columns"]):
                candidates.append(table_ddl(table, cols))
        for text in candidates:
            if estimate_text_tokens(text) <= remaining:
                parts.append(text)
                used += estimate_text_tokens(text) + 2
                break
    return "\n\n".join(parts)
