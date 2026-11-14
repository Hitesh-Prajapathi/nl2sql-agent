"""Question-aware schema context under a hard token cap (the ``*_small`` evaluation variants).

The Groq free tier allows 8,000 tokens per minute per model, so a single request (system prompt +
question + evidence + the output tokens it reserves) has to stay well below that. The full
profile of a large BIRD database does not fit, so this module picks and degrades what goes in:

1. **Rank tables** by lexical overlap of the question and the evidence with table names, column
   names (whole names and word overlap) and stored column values. Gold SQL is never used.
2. **Add foreign-key neighbours** of the matched tables (join bridges), then the rest.
3. **Fill the budget greedily** in that order. Each table is rendered at the richest level that
   fits its share (values/samples first, then dropping columns that are neither matched, primary
   key nor foreign key columns), and nothing is added once the budget is used.

``render_capped`` guarantees ``estimate_text_tokens(result) <= budget`` by construction (a final
hard truncation is the last resort and is reported by ``CappedResult.truncated``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nl2sql.config import get_settings
from nl2sql.llm.tokens import estimate_messages_tokens, estimate_text_tokens
from nl2sql.schema.introspect import SAMPLE_ROWS, render_table
from nl2sql.schema.profile import DatabaseProfile
from nl2sql.schema.store import StoredProfileSchemaProvider, load_profile

# Total request budget the small variants target: prompt + question + evidence + output tokens.
DEFAULT_TOTAL_CAP_TOKENS = 6500

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "list",
        "give",
        "show",
        "name",
        "please",
        "tell",
        "many",
        "much",
        "per",
        "each",
        "all",
        "any",
        "more",
        "most",
        "than",
        "then",
        "them",
        "they",
        "does",
        "did",
        "do",
        "not",
        "no",
        "yes",
        "both",
        "between",
        "among",
        "about",
        "into",
        "out",
        "over",
        "under",
        "also",
    ]
)
_GENERIC = frozenset(
    {"id", "name", "date", "type", "year", "number", "code", "count", "value", "values"}
    | {"total", "num", "no"}
)
_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")


def words(text: str) -> set[str]:
    """Lower-case word set with crude plural stemming and stop words removed."""
    out: set[str] = set()
    for w in _WORD.findall(_CAMEL.sub(r"\1 \2", text).lower()):
        if w in _STOP or (len(w) < 3 and not w.isdigit()):
            continue
        if len(w) > 3 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


@dataclass
class TableRank:
    name: str
    score: float = 0.0
    matched_columns: set[str] = field(default_factory=set)
    tier: str = "C"  # A: matched, B: FK neighbour of a matched table, C: the rest


def rank_tables(data: dict[str, Any], question: str, evidence: str = "") -> list[TableRank]:
    """Tables ordered by relevance to the question + evidence (see the module docstring)."""
    text = f"{question}\n{evidence}".lower()
    qwords = words(f"{question} {evidence}")
    ranks: dict[str, TableRank] = {}
    for t in data["tables"]:
        r = TableRank(t["name"])
        tw = words(t["name"])
        if tw and tw <= qwords:
            r.score += 4 * len(tw)
        elif tw & qwords:
            r.score += 2 * len(tw & qwords)
        if len(t["name"]) >= 4 and t["name"].lower() in text:
            r.score += 1
        for col in t["columns"]:
            cname = col["name"]
            cw = words(cname)
            gain = 0.0
            if len(cname) >= 4 and cname.lower() in text:
                gain = 4.0  # the exact column name appears (typical for BIRD evidence)
            elif cw and cw <= qwords:
                gain = 1.0 if cw <= _GENERIC else 3.0
            elif len(cw & qwords) >= 2 and len(cw & qwords) / len(cw) >= 0.5:
                gain = 2.0
            for v in (col.get("values") or [])[:50]:
                vl = str(v).lower()
                if len(vl) >= 3 and vl in text:
                    gain = max(gain, 3.0)
                    break
            if gain:
                r.score += gain
                r.matched_columns.add(cname)
        ranks[t["name"]] = r

    matched = {n for n, r in ranks.items() if r.score > 0}
    for n in matched:
        ranks[n].tier = "A"
    links: dict[str, int] = {}
    for t in data["tables"]:
        for fk in t.get("foreign_keys", []):
            a, b = t["name"], fk["foreign_table"]
            if a in matched and b in ranks and b not in matched:
                links[b] = links.get(b, 0) + 1
            if b in matched and a not in matched:
                links[a] = links.get(a, 0) + 1
    for n, c in links.items():
        ranks[n].tier = "B"
        ranks[n].score = c * 0.1
    tier_order = {"A": 0, "B": 1, "C": 2}
    return sorted(ranks.values(), key=lambda r: (tier_order[r.tier], -r.score, r.name))


# (max_values, n_samples, show_ranges, prune_columns, max_columns)
_LEVELS: list[tuple[int, int, bool, bool, int | None]] = [
    (30, SAMPLE_ROWS, True, False, None),
    (12, 2, True, False, None),
    (6, 1, True, False, None),
    (3, 0, True, False, None),
    (0, 0, False, False, None),
    (3, 0, True, True, None),
    (0, 0, False, True, None),
    (0, 0, False, True, 10),
    (0, 0, False, True, 5),
]
_CHECK_LEVEL = 3  # tiers B and C start here (no sample rows; at most 3 values per column)


def kept_columns(
    table: dict[str, Any], matched: set[str], max_columns: int | None = None
) -> list[dict[str, Any]]:
    """Columns that stay when pruning: matched first, then primary and foreign key columns."""
    pk = set(table.get("primary_key") or [])
    fk = {f["column"] for f in table.get("foreign_keys", [])}
    cols = table["columns"]
    wanted = [c for c in cols if c["name"] in matched or c["name"] in pk or c["name"] in fk]
    if not wanted:
        wanted = cols[:3]  # a table with nothing to anchor on still shows its first columns
    if max_columns is not None and len(wanted) > max_columns:
        prio = {c["name"]: 0 if c["name"] in matched else 1 for c in wanted}
        keep = {c["name"] for c in sorted(wanted, key=lambda c: prio[c["name"]])[:max_columns]}
        wanted = [c for c in wanted if c["name"] in keep]
    return wanted


def _render_pruned(
    table: dict[str, Any], level: tuple[int, int, bool, bool, int | None], matched: set[str]
) -> str | None:
    max_values, n_samples, ranges, _, max_cols = level
    cols = kept_columns(table, matched, max_cols)
    if len(cols) >= len(table["columns"]):
        return None  # nothing pruned: identical to an earlier level
    sub = dict(table)
    sub["columns"] = cols
    sub["samples"] = []
    text = render_table(sub, max_values, n_samples, ranges)
    omitted = len(table["columns"]) - len(cols)
    return text + f"\n  ({omitted} other columns omitted)"


def fit_table(
    table: dict[str, Any], matched: set[str], cap: int, start_level: int = 0
) -> str | None:
    """The richest rendering of ``table`` within ``cap`` tokens, or None if none fits."""
    for i in range(start_level, len(_LEVELS)):
        level = _LEVELS[i]
        if level[3]:
            text = _render_pruned(table, level, matched)
            if text is None:
                continue
        else:
            text = render_table(table, level[0], level[1], level[2])
        if estimate_text_tokens(text) <= cap:
            return text
    return None


@dataclass
class CappedResult:
    text: str
    tokens: int
    tables_included: list[str]
    tables_dropped: list[str]
    truncated: bool = False


def render_capped_result(
    data: dict[str, Any],
    question: str,
    evidence: str,
    budget_tokens: int,
    profile: DatabaseProfile | None = None,
) -> CappedResult:
    if profile is None:
        profile = DatabaseProfile(
            db_id=data["db_id"],
            display_name=data.get("display_name") or data["db_id"],
            domain_description=data.get("domain_description"),
        )
    by_name = {t["name"]: t for t in data["tables"]}
    header = profile.header()
    parts = [header]
    used = estimate_text_tokens(header)
    included: list[str] = []
    dropped: list[str] = []
    for rank in rank_tables(data, question, evidence):
        remaining = budget_tokens - used - 2
        table = by_name[rank.name]
        if rank.tier == "A":
            cap = min(remaining, max(400, int(budget_tokens * 0.4)))
            start = 0
        else:
            cap = min(remaining, max(250, int(budget_tokens * 0.2)))
            start = _CHECK_LEVEL
        text = fit_table(table, rank.matched_columns, cap, start) if remaining > 0 else None
        if text is None:
            dropped.append(rank.name)
            continue
        parts += [text, ""]
        used += estimate_text_tokens(text) + 2
        included.append(rank.name)
    result = "\n".join(parts)
    truncated = False
    if estimate_text_tokens(result) > budget_tokens:  # last resort; should not happen
        truncated = True
        result = result[: max(0, int(budget_tokens * 3.4))]
    return CappedResult(result, estimate_text_tokens(result), included, dropped, truncated)


def render_capped(
    data: dict[str, Any],
    question: str,
    evidence: str,
    budget_tokens: int,
    profile: DatabaseProfile | None = None,
) -> str:
    return render_capped_result(data, question, evidence, budget_tokens, profile).text


# ── Provider ──────────────────────────────────────────────────────────────────


def plan_prompt_overhead(question: str, evidence: str) -> int:
    """Estimated tokens of the planner request EXCLUDING the schema text."""
    from nl2sql.llm.prompts.plan_sql import build_plan_messages

    return estimate_messages_tokens(build_plan_messages("", question, evidence))


def schema_budget(
    question: str, evidence: str, total_cap_tokens: int, max_output_tokens: int
) -> int:
    """Tokens available for schema text so prompt + output stays within ``total_cap_tokens``."""
    return total_cap_tokens - max_output_tokens - plan_prompt_overhead(question, evidence)


class CappedProfileSchemaProvider(StoredProfileSchemaProvider):
    """Stored profile rendered per question under ``total_cap_tokens`` (see module docstring)."""

    def __init__(
        self,
        data: dict[str, Any],
        total_cap_tokens: int = DEFAULT_TOTAL_CAP_TOKENS,
        max_output_tokens: int | None = None,
    ) -> None:
        super().__init__(data)
        self.total_cap_tokens = total_cap_tokens
        self.max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else get_settings().planner_max_tokens
        )

    @classmethod
    def from_capped_file(
        cls,
        path: str | Path,
        total_cap_tokens: int = DEFAULT_TOTAL_CAP_TOKENS,
        max_output_tokens: int | None = None,
    ) -> CappedProfileSchemaProvider:
        return cls(load_profile(path), total_cap_tokens, max_output_tokens)

    async def get_context(self, question: str = "", evidence: str = "") -> str:
        budget = schema_budget(question, evidence, self.total_cap_tokens, self.max_output_tokens)
        return render_capped(self._data, question, evidence, max(budget, 200), self.profile)
