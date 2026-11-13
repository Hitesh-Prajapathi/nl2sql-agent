"""Unit tests for every routing function and terminal node (no LLM, no DB)."""
from __future__ import annotations

import pytest

from nl2sql.agent import graph as g
from nl2sql.agent.nodes import execute as ex


@pytest.fixture(autouse=True)
def _budget(set_settings):
    set_settings(max_retry_attempts=3)


# ── route_after_plan ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "decision,expected",
    [("clarify", "clarify"), ("refuse", "refuse"), ("sql", "validate"), (None, "validate")],
)
def test_route_after_plan(decision, expected):
    assert g.route_after_plan({"decision": decision}) == expected


# ── route_after_validation ───────────────────────────────────────────────────

def test_validation_blocked_is_terminal_even_with_budget():
    assert g.route_after_validation({"is_blocked": True, "attempt_count": 0}) == "blocked"


def test_validation_valid_executes():
    assert g.route_after_validation({"is_valid_sql": True, "attempt_count": 0}) == "execute"


def test_validation_invalid_with_budget_corrects():
    assert g.route_after_validation({"is_valid_sql": False, "attempt_count": 2}) == "correct"


def test_validation_invalid_budget_exhausted_fails():
    assert g.route_after_validation({"is_valid_sql": False, "attempt_count": 3}) == "max_retries"


def test_validation_invalid_budget_zero_fails_immediately(set_settings):
    set_settings(max_retry_attempts=0)
    assert g.route_after_validation({"is_valid_sql": False, "attempt_count": 0}) == "max_retries"


# ── route_after_execution ────────────────────────────────────────────────────

def test_execution_routes():
    assert g.route_after_execution({"exec_error": ""}) == "success"
    assert g.route_after_execution({}) == "success"
    assert g.route_after_execution({"exec_error": "boom"}) == "error"


# ── route_after_correction ───────────────────────────────────────────────────

def test_correction_safety_refusal_ends():
    assert g.route_after_correction({"status": "refused", "attempt_count": 1}) == "end"


def test_correction_status_failed_fails():
    assert g.route_after_correction({"status": "failed", "attempt_count": 0}) == "fail"


def test_correction_after_last_allowed_call_still_validates():
    # attempt_count == max means the last allowed correction just ran: run its SQL.
    assert g.route_after_correction({"status": "answered", "attempt_count": 3}) == "validate"


def test_correction_normal_validates():
    assert g.route_after_correction({"status": "answered", "attempt_count": 1}) == "validate"


def test_correction_overrun_fails():
    assert g.route_after_correction({"status": "answered", "attempt_count": 4}) == "fail"


# ── terminal nodes ───────────────────────────────────────────────────────────

def test_clarify_node():
    out = g.clarify_node({"clarification_question": "Which month?"})
    assert out["status"] == "needs_clarification"
    assert "Which month?" in out["final_answer"]
    assert out["final_sql"] is None


def test_refuse_node_planner_reason():
    out = g.refuse_node({"refuse_reason": "Writes are not allowed."})
    assert out["status"] == "refused"
    assert "Writes are not allowed." in out["final_answer"]
    assert out["final_sql"] is None


def test_refuse_node_preserves_existing_answer():
    out = g.refuse_node({"final_answer": "custom", "status": "refused"})
    assert out["final_answer"] == "custom"


def test_refuse_node_preserves_failed_status():
    out = g.refuse_node({"status": "failed", "final_answer": "LLM down"})
    assert out["status"] == "failed"


def test_refuse_node_blocked_includes_validation_error():
    out = g.refuse_node(
        {"is_blocked": True, "validation_error": "Only SELECT statements are allowed. Got: Drop",
         "status": "answered", "final_answer": ""}
    )
    assert out["status"] == "refused"
    assert "Only SELECT statements are allowed" in out["final_answer"]
    assert out["final_sql"] is None


def test_max_retries_node_reports_last_error():
    out = g.max_retries_node(
        {"attempt_count": 3, "exec_error": "Column not found: foo", "generated_sql": "SELECT foo"}
    )
    assert out["status"] == "failed"
    assert "Column not found: foo" in out["final_answer"]
    assert out["final_sql"] == "SELECT foo"


def test_max_retries_node_falls_back_to_validation_then_attempts():
    out = g.max_retries_node({"validation_error": "SQL syntax error: x"})
    assert "SQL syntax error: x" in out["final_answer"]
    out = g.max_retries_node({"attempts": [{"error": "old error"}]})
    assert "old error" in out["final_answer"]
    out = g.max_retries_node({})
    assert out["status"] == "failed" and "Unknown error" in out["final_answer"]


# ── correct_sql_node budget / error selection (B3, B4) ───────────────────────

async def test_correct_node_budget_zero_makes_no_llm_call(fake_llm, set_settings):
    set_settings(max_retry_attempts=0)
    out = await ex.correct_sql_node(
        {"question": "q", "schema_context": "s", "generated_sql": "SELECT 1",
         "exec_error": "Column not found: x", "attempt_count": 0},
        config={},
    )
    assert out["status"] == "failed"
    assert fake_llm.count("correct") == 0


async def test_correct_node_uses_validation_error_when_exec_error_empty(fake_llm):
    fake_llm.correct("SELECT 2")
    out = await ex.correct_sql_node(
        {"question": "q", "schema_context": "s", "generated_sql": "SELEC 1",
         "exec_error": "", "validation_error": "SQL syntax error: near SELEC",
         "attempt_count": 0},
        config={},
    )
    assert out["generated_sql"] == "SELECT 2"
    assert out["attempts"][0]["error"] == "SQL syntax error: near SELEC"
    assert "SQL syntax error: near SELEC" in fake_llm.calls_for("correct")[0].text


async def test_correct_node_unknown_error_default(fake_llm):
    out = await ex.correct_sql_node(
        {"question": "q", "schema_context": "s", "generated_sql": "x", "attempt_count": 0},
        config={},
    )
    assert out["attempts"][0]["error"] == "Unknown error"


async def test_correct_node_safety_is_terminal_even_with_zero_budget(fake_llm, set_settings):
    set_settings(max_retry_attempts=0)
    out = await ex.correct_sql_node(
        {"question": "q", "schema_context": "s", "generated_sql": "x",
         "exec_error": "Write operation blocked", "is_safety_exec_block": True,
         "attempt_count": 0},
        config={},
    )
    assert out["status"] == "refused"
    assert fake_llm.count("correct") == 0
