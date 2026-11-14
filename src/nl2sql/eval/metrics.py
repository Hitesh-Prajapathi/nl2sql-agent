"""BIRD Mini-Dev metrics: EX, Soft F1, R-VES, and schema-linking scores.

EX, Soft F1 and the R-VES reward/aggregation are ports of the official evaluation scripts in
``bird-bench/mini_dev`` (``evaluation/evaluation_{ex,f1,ves}.py``, repository commit
``1c49a375e76c471e218564da3f4617513d33e371``). Semantics kept:

* EX = 1 iff ``set(pred_rows) == set(gold_rows)`` (rows as tuples; order and duplicates ignored;
  a prediction that errors or times out scores 0).
* Soft F1: both row lists are de-duplicated preserving order, then compared POSITIONALLY row by
  row (per-row value-membership overlap), exactly as the official script does. Both empty = 1.0.
* R-VES: only for EX-correct predictions, ``time_ratio = mean(gold_time / pred_time)`` over
  repeated runs after dropping outside-3-sigma ratios; reward 1.25 / 1 / 0.75 / 0.5 / 0.25 by
  ratio bands; the score is ``mean(sqrt(reward)) * 100`` over ALL questions (incorrect = 0).

Documented deviations: the official script repeats each query 100 times (``iterate_num``) and times
connection open + fetch; here the repeat count is configurable and only execute + fetch is timed.
R-VES on a shared database is noisy; treat it as indicative.
"""

from __future__ import annotations

import math
import re
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

OFFICIAL_EVAL_COMMIT = "1c49a375e76c471e218564da3f4617513d33e371"

Row = tuple[Hashable, ...]


# ── Row normalisation ─────────────────────────────────────────────────────────


def _hashable(value: Any) -> Hashable:
    if isinstance(value, list | tuple):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((str(k), _hashable(v)) for k, v in value.items()))
    if isinstance(value, set | frozenset):
        return tuple(sorted((_hashable(v) for v in value), key=repr))
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    return value  # type: ignore[no-any-return]


def normalize_rows(rows: Iterable[Sequence[Any]]) -> list[Row]:
    return [tuple(_hashable(v) for v in row) for row in rows]


# ── EX ────────────────────────────────────────────────────────────────────────


def execution_match(pred: Sequence[Row], gold: Sequence[Row]) -> bool:
    return set(pred) == set(gold)


# ── Soft F1 ───────────────────────────────────────────────────────────────────


def _row_match(pred_row: Row, gold_row: Row) -> tuple[float, float, float]:
    total = len(gold_row)
    matches = pred_only = gold_only = 0
    for v in pred_row:
        if v in gold_row:
            matches += 1
        else:
            pred_only += 1
    for v in gold_row:
        if v not in pred_row:
            gold_only += 1
    return matches / total, pred_only / total, gold_only / total


def soft_f1(pred: Sequence[Row], gold: Sequence[Row]) -> float:
    if not pred and not gold:
        return 1.0
    p = list(dict.fromkeys(pred))
    g = list(dict.fromkeys(gold))
    match_scores: list[float] = []
    pred_only: list[float] = []
    gold_only: list[float] = []
    for i, gold_row in enumerate(g):
        if i >= len(p):
            match_scores.append(0)
            gold_only.append(1)
            continue
        m, po, go = _row_match(p[i], gold_row)
        match_scores.append(m)
        pred_only.append(po)
        gold_only.append(go)
    for _ in range(len(p) - len(g)):
        match_scores.append(0)
        pred_only.append(1)
        gold_only.append(0)
    tp, fp, fn = sum(match_scores), sum(pred_only), sum(gold_only)
    precision = tp / (tp + fp) if tp + fp > 0 else 0
    recall = tp / (tp + fn) if tp + fn > 0 else 0
    return 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0


# ── R-VES ─────────────────────────────────────────────────────────────────────


def ves_reward(time_ratio: float) -> float:
    """Reward bands from the official script (time_ratio = gold_time / pred_time)."""
    if time_ratio <= 0:
        return 0.0
    if time_ratio >= 2:
        return 1.25
    if time_ratio >= 1:
        return 1.0
    if time_ratio >= 0.5:
        return 0.75
    if time_ratio >= 0.25:
        return 0.5
    return 0.25


def _drop_outliers(ratios: Sequence[float]) -> list[float]:
    n = len(ratios)
    mean = sum(ratios) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in ratios) / n)
    kept = [x for x in ratios if mean - 3 * std < x < mean + 3 * std]
    # With zero spread the strict inequalities drop everything; the mean is then the value itself.
    return kept or list(ratios)


def ves_time_ratio(gold_times: Sequence[float], pred_times: Sequence[float]) -> float:
    """Mean of per-repeat gold/pred ratios after the official outlier filter."""
    ratios = [g / p for g, p in zip(gold_times, pred_times, strict=True) if p > 0]
    if not ratios:
        return 0.0
    kept = _drop_outliers(ratios)
    return sum(kept) / len(kept)


def ves_score(rewards: Sequence[float]) -> float:
    """Official aggregation: mean(sqrt(reward)) * 100 over all questions."""
    if not rewards:
        return 0.0
    return sum(math.sqrt(r) * 100 for r in rewards) / len(rewards)


# ── Schema linking (gold side via sqlglot) ────────────────────────────────────


@dataclass(frozen=True)
class GoldLinks:
    tables: frozenset[str]
    columns: frozenset[str]  # "table.column", lower-case
    unresolved: int  # column references that could not be tied to a base table


def gold_links(
    gold_sql: str, schema: dict[str, list[str]] | None = None
) -> GoldLinks | None:
    """Tables and ``table.column`` pairs referenced by ``gold_sql``; None if it cannot be parsed.

    ``schema`` maps lower-case table -> lower-case columns. It resolves unqualified columns to the
    query's base tables. Aliases (``T1``) are resolved through the AST; CTE / derived-table
    references are not base tables and count as ``unresolved``.
    """
    try:
        tree = sqlglot.parse_one(gold_sql, dialect="postgres")
    except sqlglot.errors.SqlglotError:
        return None
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    alias_to_table: dict[str, str] = {}
    tables: set[str] = set()
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if not name or name in ctes:
            continue
        tables.add(name)
        alias_to_table[(t.alias_or_name or name).lower()] = name
        alias_to_table[name] = name
    columns: set[str] = set()
    unresolved = 0
    for col in tree.find_all(exp.Column):
        cname = col.name.lower()
        if not cname or isinstance(col.this, exp.Star):
            continue
        qualifier = col.table.lower()
        if qualifier:
            table = alias_to_table.get(qualifier)
            if table is None:
                unresolved += 1
                continue
            columns.add(f"{table}.{cname}")
        else:
            owners = [t for t in sorted(tables) if schema and cname in schema.get(t, [])]
            if owners:
                columns.add(f"{owners[0]}.{cname}")
            elif schema is None and len(tables) == 1:
                columns.add(f"{next(iter(tables))}.{cname}")
            else:
                unresolved += 1  # output alias, or a column of a derived table
    return GoldLinks(frozenset(tables), frozenset(columns), unresolved)


_QUOTES = re.compile(r'["`\[\]]')


def parse_predicted_links(
    entries: Iterable[str], known_tables: set[str]
) -> tuple[set[str], set[str]]:
    """Planner ``relevant_tables`` ("table.column" strings) -> (tables, table.column pairs)."""
    tables: set[str] = set()
    columns: set[str] = set()
    for raw in entries:
        text = _QUOTES.sub("", str(raw)).strip().lower()
        if not text:
            continue
        if "." in text:
            table, _, column = text.rpartition(".")
            table = table.rpartition(".")[2]  # drop any schema prefix
            tables.add(table)
            if column and column != "*":
                columns.add(f"{table}.{column}")
        elif text in known_tables:
            tables.add(text)
    return tables, columns


def set_precision_recall(pred: set[str], gold: frozenset[str] | set[str]) -> tuple[float | None, float | None]:
    """(precision, recall); None when the corresponding denominator is empty."""
    hit = len(pred & gold)
    precision = hit / len(pred) if pred else None
    recall = hit / len(gold) if gold else None
    return precision, recall
