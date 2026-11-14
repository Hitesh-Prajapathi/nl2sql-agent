"""Offline (no LLM, no database) prompt-size computation for every evaluation variant.

For each question it builds the exact planner request the variant would send (the same functions
the graph node and the naive variant call), estimates its tokens with the conservative estimator
in ``nl2sql.llm.tokens`` and adds the output tokens the call reserves. For the graph variants it
also sizes the worst-case SQL-correction request (failed SQL and error at their truncation
bounds), because that call carries the same schema text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nl2sql.agent.nodes.execute import (
    CORRECTION_INSTRUCTIONS,
    MAX_CORRECTION_ERROR_CHARS,
    MAX_CORRECTION_SQL_CHARS,
)
from nl2sql.eval.dataset import Question
from nl2sql.eval.stats import percentile
from nl2sql.eval.variants import SMALL_VARIANTS, VARIANTS, naive_messages
from nl2sql.llm.prompts.plan_sql import CORRECTION_SYSTEM, build_plan_messages
from nl2sql.llm.tokens import estimate_messages_tokens
from nl2sql.schema.capped import DEFAULT_TOTAL_CAP_TOKENS, render_capped_result, schema_budget
from nl2sql.schema.ddl import build_ddl, build_ddl_capped
from nl2sql.schema.introspect import render_context
from nl2sql.schema.profile import DatabaseProfile

DEFAULT_TABLE_BUDGET = 1500


@dataclass
class PromptSize:
    question_id: int
    db_id: str
    prompt_tokens: int  # planner request (system + user), estimated
    total_tokens: int  # prompt_tokens + max output tokens (the amount reserved against the TPM)
    correction_total_tokens: (
        int  # worst-case correction request + its output reservation (0 = none)
    )
    tables_included: int = 0
    tables_total: int = 0
    truncated: bool = False


def _profile_of(data: dict[str, Any]) -> DatabaseProfile:
    return DatabaseProfile(
        db_id=data["db_id"],
        display_name=data.get("display_name") or data["db_id"],
        domain_description=data.get("domain_description"),
        date_ranges={k: (v[0], v[1]) for k, v in data.get("date_ranges", {}).items()},
    )


def compute_prompt_sizes(
    variant: str,
    questions: list[Question],
    profiles: dict[str, dict[str, Any]],
    *,
    use_evidence: bool = True,
    total_cap_tokens: int = DEFAULT_TOTAL_CAP_TOKENS,
    max_output_tokens: int = 2048,
    table_budget: int = DEFAULT_TABLE_BUDGET,
) -> list[PromptSize]:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    small = variant in SMALL_VARIANTS
    graph = variant.startswith("full")
    full_ctx = {d: render_context(p, table_budget, _profile_of(p)) for d, p in profiles.items()}
    full_ddl = {d: build_ddl(p) for d, p in profiles.items()}
    worst_instruction = max(CORRECTION_INSTRUCTIONS.values(), key=len)
    out: list[PromptSize] = []
    for q in questions:
        evidence = q.evidence if use_evidence else ""
        data = profiles[q.db_id]
        included = truncated = 0
        if graph:
            if small:
                budget = schema_budget(q.question, evidence, total_cap_tokens, max_output_tokens)
                res = render_capped_result(
                    data, q.question, evidence, max(budget, 200), _profile_of(data)
                )
                schema, included, truncated = res.text, len(res.tables_included), res.truncated
            else:
                schema, included = full_ctx[q.db_id], len(data["tables"])
            prompt = estimate_messages_tokens(build_plan_messages(schema, q.question, evidence))
            corr_system = CORRECTION_SYSTEM.format(
                question=q.question,
                failed_sql=q.gold_sql[:MAX_CORRECTION_SQL_CHARS],
                error="x" * MAX_CORRECTION_ERROR_CHARS,
                correction_instruction=worst_instruction,
                schema_context=schema,
            )
            corr = estimate_messages_tokens(
                [
                    {"role": "system", "content": corr_system},
                    {"role": "user", "content": "Return ONLY the corrected SQL query."},
                ]
            )
            corr_total = corr + max_output_tokens
        else:
            if small:
                overhead = estimate_messages_tokens(naive_messages("", q.question, evidence))
                budget = total_cap_tokens - max_output_tokens - overhead
                ddl = build_ddl_capped(data, q.question, evidence, budget)
                included = ddl.count("CREATE TABLE")
            else:
                ddl, included = full_ddl[q.db_id], len(data["tables"])
            prompt = estimate_messages_tokens(naive_messages(ddl, q.question, evidence))
            corr_total = 0
        out.append(
            PromptSize(
                q.question_id,
                q.db_id,
                prompt,
                prompt + max_output_tokens,
                corr_total,
                included,
                len(data["tables"]),
                bool(truncated),
            )
        )
    return out


def size_stats(sizes: list[PromptSize], cap: int | None) -> dict[str, Any]:
    """max / mean / P95 of the total reserved tokens (prompt + max output) per request."""
    totals = [float(s.total_tokens) for s in sizes]
    corr = [float(s.correction_total_tokens) for s in sizes if s.correction_total_tokens]
    prompts = [float(s.prompt_tokens) for s in sizes]
    return {
        "n": len(sizes),
        "cap_total_tokens": cap,
        "max_total_tokens": int(max(totals)) if totals else 0,
        "mean_total_tokens": round(sum(totals) / len(totals), 1) if totals else 0.0,
        "p95_total_tokens": round(percentile(totals, 95) or 0.0, 1),
        "max_prompt_tokens": int(max(prompts)) if prompts else 0,
        "mean_prompt_tokens": round(sum(prompts) / len(prompts), 1) if prompts else 0.0,
        "p95_prompt_tokens": round(percentile(prompts, 95) or 0.0, 1),
        "max_correction_total_tokens": int(max(corr)) if corr else 0,
        "over_cap": sum(1 for t in totals if cap is not None and t > cap),
        "estimator": "ascii chars/3.5 + 1 per non-ascii char + 8 per message (conservative)",
    }
