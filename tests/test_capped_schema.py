"""Question-aware schema cap (``*_small`` variants): ranking, pruning, budget, DDL, 500 check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nl2sql.eval.dataset import load_questions
from nl2sql.eval.promptsize import compute_prompt_sizes, size_stats
from nl2sql.llm.prompts.plan_sql import build_plan_messages
from nl2sql.llm.tokens import estimate_messages_tokens, estimate_text_tokens
from nl2sql.schema.capped import (
    CappedProfileSchemaProvider,
    kept_columns,
    rank_tables,
    render_capped,
    render_capped_result,
    schema_budget,
    words,
)
from nl2sql.schema.ddl import build_ddl, build_ddl_capped
from nl2sql.schema.store import load_profile, profile_path


def col(name: str, typ: str = "text", values: list[str] | None = None) -> dict[str, Any]:
    c: dict[str, Any] = {"name": name, "type": typ, "nullable": True}
    if values:
        c["values"] = values
        c["values_complete"] = True
    return c


def table(name: str, cols: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {
        "name": name,
        "rows": 100,
        "rows_estimated": False,
        "columns": cols,
        "primary_key": kw.get("pk", []),
        "foreign_keys": kw.get("fks", []),
        "checks": [],
        "samples": [{c["name"]: "sample-value-" + c["name"] for c in cols}] * 3,
    }


def profile() -> dict[str, Any]:
    wide = table(
        "match",
        [col("id", "integer")] + [col(f"stat_{i}", "integer") for i in range(120)],
        pk=["id"],
    )
    return {
        "db_id": "toy",
        "display_name": "toy",
        "tables": [
            table(
                "schools",
                [col("cds", "text"), col("county", "text", ["Alameda", "Fresno", "Ontario"])],
                pk=["cds"],
            ),
            table(
                "scores",
                [col("cds", "text"), col("avg_math_score", "integer")],
                fks=[{"column": "cds", "foreign_table": "schools", "foreign_column": "cds"}],
            ),
            table("lookup", [col("code"), col("label")]),
            table(
                "unrelated_bridge",
                [col("scores_cds"), col("lookup_code")],
                fks=[
                    {"column": "scores_cds", "foreign_table": "scores", "foreign_column": "cds"},
                    {"column": "lookup_code", "foreign_table": "lookup", "foreign_column": "code"},
                ],
            ),
            wide,
        ],
    }


def test_words_stems_and_drops_stop_words() -> None:
    assert words("The Schools in Alameda County") == {"school", "alameda", "county"}
    assert words("avg_math_score") == {"avg", "math", "score"}
    assert "id" not in words("the id of it")


def test_ranking_uses_question_evidence_values_and_fk_neighbours() -> None:
    ranks = {
        r.name: r for r in rank_tables(profile(), "Which schools in Ontario have math scores?", "")
    }
    assert ranks["schools"].tier == "A" and ranks["scores"].tier == "A"
    assert "county" in ranks["schools"].matched_columns  # matched through the stored value
    assert ranks["unrelated_bridge"].tier == "B"  # joins matched tables, not matched itself
    assert ranks["lookup"].tier in ("B", "C") and ranks["match"].tier == "C"
    order = [r.name for r in rank_tables(profile(), "Which schools in Ontario have math scores?")]
    assert order.index("schools") < order.index("unrelated_bridge") < order.index("match")


def test_evidence_naming_a_column_pulls_its_table_in() -> None:
    ev = "lookup label refers to label = 'x'"
    ranks = {r.name: r for r in rank_tables(profile(), "Describe things.", ev)}
    assert ranks["lookup"].tier == "A" and "label" in ranks["lookup"].matched_columns


def test_wide_table_is_pruned_to_matched_pk_and_fk_columns() -> None:
    p = profile()
    wide = p["tables"][-1]
    cols = kept_columns(wide, {"stat_7", "stat_9"})
    assert [c["name"] for c in cols] == ["id", "stat_7", "stat_9"]
    text = render_capped(p, "average stat_7 of match", "", 1500)
    assert "stat_7" in text and "stat_100" not in text and "other columns omitted" in text


@pytest.mark.parametrize("budget", [400, 800, 1500, 3000])
def test_output_never_exceeds_the_budget(budget: int) -> None:
    for q in ("schools in Ontario", "stat_5 and stat_6 of match", "", "everything about lookup"):
        res = render_capped_result(profile(), q, "", budget)
        assert res.tokens <= budget, (budget, q, res.tokens)
        assert estimate_text_tokens(res.text) == res.tokens


def test_smaller_budget_keeps_the_most_relevant_table() -> None:
    res = render_capped_result(profile(), "schools in Ontario", "", 450)
    assert res.tables_included[0] == "schools"
    assert "stat_100" not in res.text  # the irrelevant wide table is cut down to its key


def test_ddl_cap_prefers_relevant_tables_and_respects_budget() -> None:
    p = profile()
    full = build_ddl(p)
    capped = build_ddl_capped(p, "stat_3 of match", "", 120)
    assert estimate_text_tokens(capped) <= 120 < estimate_text_tokens(full)
    assert 'CREATE TABLE "match"' in capped and '"stat_3"' in capped
    assert '"stat_100"' not in capped


async def test_provider_budget_accounts_for_template_question_and_evidence() -> None:
    p = CappedProfileSchemaProvider(profile(), total_cap_tokens=6500, max_output_tokens=2048)
    q, ev = "schools in Ontario", "Ontario refers to county = 'Ontario'"
    ctx = await p.get_context(q, evidence=ev)
    total = estimate_messages_tokens(build_plan_messages(ctx, q, ev)) + 2048
    assert total <= 6500
    assert schema_budget(q, ev, 6500, 2048) >= estimate_text_tokens(ctx)


BIRD = Path("data/bird_minidev/raw/minidev/MINIDEV/mini_dev_postgresql.json")
PROFILES = Path("data/profiles")


@pytest.mark.skipif(
    not BIRD.exists() or not (PROFILES / "financial.json").exists(),
    reason="BIRD data / profiles not present (they are gitignored; run `make bird-setup profile`)",
)
@pytest.mark.parametrize("variant", ["full_small", "naive_small"])
def test_all_500_questions_fit_under_the_cap(variant: str) -> None:
    qs = load_questions("data/bird_minidev")
    profs = {d: load_profile(profile_path(d, PROFILES)) for d in {q.db_id for q in qs}}
    sizes = compute_prompt_sizes(variant, qs, profs, total_cap_tokens=6500, max_output_tokens=2048)
    st = size_stats(sizes, 6500)
    assert st["n"] == 500 and st["over_cap"] == 0 and st["max_total_tokens"] <= 6500
    assert st["max_correction_total_tokens"] <= 6500
    assert not any(s.truncated for s in sizes)
