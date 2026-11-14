"""Canned plans and sampled latencies for the fake LLM used in load tests.

LATENCY DEFAULTS ARE ASSUMPTIONS (not measurements). They are plausible figures for a
70B planner call and an 8B synthesis call on a hosted provider, to be replaced with
latencies sampled from real Langfuse traces. Override with env vars:

    LT_PLAN_MEDIAN_MS (900)   LT_PLAN_P95_MS (2500)    planner (70B) calls
    LT_FAST_MEDIAN_MS (300)   LT_FAST_P95_MS (800)     synthesis (8B) calls
    LT_LATENCY_SEED           optional RNG seed

Latency ~ lognormal with mu = ln(median), sigma = ln(P95 / median) / 1.645.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass
from typing import Any

from nl2sql.models import PlanResult

# ── Canned SQL (all valid against db/schema.sql) ─────────────────────────────
SIMPLE: dict[str, str] = {
    "How many customers are there in each tier?": "SELECT tier, COUNT(*) AS customers FROM customers GROUP BY tier ORDER BY tier",
    "What is the distribution of orders by status?": "SELECT status, COUNT(*) AS orders FROM orders GROUP BY status ORDER BY orders DESC",
    "What are the 5 most expensive products and their prices?": "SELECT name, unit_price FROM products ORDER BY unit_price DESC LIMIT 5",
    "What is the total amount of completed payments broken down by payment method?": "SELECT method, SUM(amount) AS total FROM payments WHERE status = 'completed' GROUP BY method ORDER BY total DESC",
    "Which customers have placed more than 3 orders, and how many did they place?": "SELECT c.name, COUNT(*) AS orders FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name HAVING COUNT(*) > 3 ORDER BY orders DESC",
    "What is the total quantity sold for each product category?": "SELECT p.category, SUM(oi.quantity) AS units FROM order_items oi JOIN products p ON p.product_id = oi.product_id GROUP BY p.category ORDER BY units DESC",
    "How many orders were placed in each city?": "SELECT shipping_city, COUNT(*) AS orders FROM orders GROUP BY shipping_city ORDER BY orders DESC",
}

COMPLEX: dict[str, str] = {
    "What are the top 3 product categories by total units sold on delivered orders?": "SELECT p.category, SUM(oi.quantity) AS units FROM order_items oi JOIN orders o ON o.order_id = oi.order_id JOIN products p ON p.product_id = oi.product_id WHERE o.status = 'delivered' GROUP BY p.category ORDER BY units DESC LIMIT 3",
    "For each returned order, what was the total refund amount and what percentage of the order total was refunded?": "SELECT o.order_id, SUM(r.amount) AS refunded, ROUND(100.0 * SUM(r.amount) / o.total_amount, 1) AS pct FROM orders o JOIN payments p ON p.order_id = o.order_id JOIN refunds r ON r.payment_id = p.payment_id WHERE o.status = 'returned' GROUP BY o.order_id, o.total_amount ORDER BY o.order_id",
    "Which orders were paid less than their total amount, and by how much?": "SELECT o.order_id, o.total_amount, COALESCE(SUM(p.amount), 0) AS paid, o.total_amount - COALESCE(SUM(p.amount), 0) AS shortfall FROM orders o LEFT JOIN payments p ON p.order_id = o.order_id AND p.status = 'completed' GROUP BY o.order_id, o.total_amount HAVING COALESCE(SUM(p.amount), 0) < o.total_amount ORDER BY shortfall DESC LIMIT 100",
    "How many open or escalated support tickets does each agent team have?": "SELECT a.team, COUNT(*) AS tickets FROM support_tickets t JOIN agents a ON a.agent_id = t.agent_id WHERE t.status IN ('open', 'escalated') GROUP BY a.team ORDER BY tickets DESC",
    "Which customers have submitted support tickets but have never placed an order?": "SELECT c.customer_id, c.name FROM customers c WHERE EXISTS (SELECT 1 FROM support_tickets t WHERE t.customer_id = c.customer_id) AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id) ORDER BY c.customer_id",
    "Which orders are marked cancelled but have completed payments?": "SELECT DISTINCT o.order_id, o.total_amount FROM orders o JOIN payments p ON p.order_id = o.order_id WHERE o.status = 'cancelled' AND p.status = 'completed' ORDER BY o.order_id",
}

# (turn 1, turn 2): turn 2 reuses the thread_id so the session store is exercised.
FOLLOWUPS: list[tuple[str, str]] = [
    ("What is the distribution of orders by status?", "Now just show the cancelled ones"),
    (
        "What are the 5 most expensive products and their prices?",
        "What is the average price of those?",
    ),
    ("How many customers are there in each tier?", "Which of those tiers has the most customers?"),
]
FOLLOWUP_SQL: dict[str, str] = {
    "Now just show the cancelled ones": "SELECT order_id, customer_id, order_date, total_amount FROM orders WHERE status = 'cancelled' ORDER BY order_date DESC LIMIT 100",
    "What is the average price of those?": "SELECT AVG(unit_price) AS avg_price FROM (SELECT unit_price FROM products ORDER BY unit_price DESC LIMIT 5) t",
    "Which of those tiers has the most customers?": "SELECT tier, COUNT(*) AS customers FROM customers GROUP BY tier ORDER BY customers DESC LIMIT 1",
}

ALL_SQL: dict[str, str] = {**SIMPLE, **COMPLEX, **FOLLOWUP_SQL}
DEFAULT_SQL = "SELECT COUNT(*) AS total FROM customers"


@dataclass(frozen=True)
class LatencyParams:
    plan_median_ms: float
    plan_p95_ms: float
    fast_median_ms: float
    fast_p95_ms: float

    @classmethod
    def from_env(cls) -> LatencyParams:
        e = os.environ.get
        return cls(
            plan_median_ms=float(e("LT_PLAN_MEDIAN_MS", "900")),
            plan_p95_ms=float(e("LT_PLAN_P95_MS", "2500")),
            fast_median_ms=float(e("LT_FAST_MEDIAN_MS", "300")),
            fast_p95_ms=float(e("LT_FAST_P95_MS", "800")),
        )

    def sampler(self, channel: str, rng: random.Random) -> float:
        median, p95 = (
            (self.plan_median_ms, self.plan_p95_ms)
            if channel in ("plan", "correct")
            else (self.fast_median_ms, self.fast_p95_ms)
        )
        sigma = math.log(p95 / median) / 1.645
        return rng.lognormvariate(math.log(median), sigma)


def _last_user_text(messages: Any) -> str:
    if isinstance(messages, str):
        return messages
    for m in reversed(list(messages)):
        if isinstance(m, dict) and m.get("role") in ("user", "human"):
            return str(m.get("content", ""))
    return ""


def plan_for(question: str) -> PlanResult:
    return PlanResult(
        relevant_tables=["customers.customer_id"],
        ambiguity_analysis="None",
        decision="sql",
        sql=ALL_SQL.get(question.strip(), DEFAULT_SQL),
        assumptions=[],
    )


def install(script: Any, params: LatencyParams, seed: int | None = None) -> None:
    """Make a FakeLLMScript answer from the canned map with lognormal latency.

    Replaces the script's queue lookup (and its unbounded call log) on this instance only.
    """
    rng = random.Random(seed)
    current = {"channel": "plan"}

    def _next(channel: str, messages: Any) -> Any:
        current["channel"] = channel
        if channel == "plan":
            return plan_for(_last_user_text(messages))
        if channel == "correct":
            return DEFAULT_SQL
        return "Canned summary from the fake LLM."

    def _latency() -> float:
        return params.sampler(current["channel"], rng) / 1000.0

    script._next = _next
    script._effective_latency_s = _latency
