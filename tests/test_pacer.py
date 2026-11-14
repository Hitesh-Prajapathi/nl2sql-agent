"""Token-bucket pacer, daily request budget, 429 handling and model attribution (offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nl2sql.eval.llm_hook import CachedLLM, DiskCache, UsageCollector, current_usage
from nl2sql.llm import pacer as pacer_mod
from nl2sql.llm import providers
from nl2sql.llm.pacer import (
    DailyBudgetExhausted,
    Pacer,
    RequestTooLarge,
    parse_duration,
    parse_rate_limit,
)
from nl2sql.llm.providers import FakeChatModel, FakeLLMScript, LLMHTTPError, ResilientChatModel
from tests.fakes import SetSettings


class Clock:
    """Fake monotonic + UTC clock; ``sleep`` advances both and records the request."""

    def __init__(self) -> None:
        self.t = 0.0
        self.utc = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def mono(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self.utc + timedelta(seconds=self.t)

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make_pacer(clock: Clock, **kw: Any) -> Pacer:
    kw.setdefault("rpd_margin", 0)
    return Pacer(clock=clock.mono, utcnow=clock.now, sleep=clock.sleep, **kw)


class RateLimited(Exception):
    status_code = 429

    def __init__(self, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)

        class _Resp:
            pass

        self.response = _Resp()
        self.response.headers = headers or {}  # type: ignore[attr-defined]


def test_parse_duration_formats() -> None:
    assert parse_duration("7.66s") == pytest.approx(7.66)
    assert parse_duration("2m59.56s") == pytest.approx(179.56)
    assert parse_duration("1h2m") == 3720
    assert parse_duration("120ms") == pytest.approx(0.12)
    assert parse_duration("30") == 30.0
    assert parse_duration(None) is None and parse_duration("soon") is None


def test_parse_rate_limit_minute_vs_daily() -> None:
    minute = parse_rate_limit(
        RateLimited("Rate limit reached ... tokens per minute (TPM)", {"retry-after": "12"})
    )
    assert minute.retry_after_s == 12 and not minute.daily
    daily = parse_rate_limit(
        RateLimited("Rate limit reached ... requests per day (RPD): Limit 1000", {})
    )
    assert daily.daily
    huge = parse_rate_limit(RateLimited("slow down", {"retry-after": "40000"}))
    assert huge.daily  # an absurd wait is the daily limit, not something to sleep through
    assert parse_rate_limit(RuntimeError("no response")).retry_after_s is None


async def test_bucket_waits_for_refill_and_settle_refunds_unused_reservation() -> None:
    c = Clock()
    p = make_pacer(c, tpm=6000)  # 100 tokens/s
    await p.acquire("m", 4000)
    assert c.sleeps == []  # the bucket starts full
    await p.acquire("m", 4000)  # 2000 left, needs 2000 more -> 20 s
    assert sum(c.sleeps) == pytest.approx(20, abs=0.5)
    c.sleeps.clear()
    p.settle("m", reserved=4000, actual=500)  # most of the reservation comes back
    await p.acquire("m", 3000)
    assert c.sleeps == []
    # separate models have separate buckets
    await p.acquire("other", 6000)
    assert c.sleeps == []


async def test_request_larger_than_the_bucket_is_refused_not_awaited() -> None:
    c = Clock()
    p = make_pacer(c, tpm=8000)
    with pytest.raises(RequestTooLarge) as ei:
        await p.acquire("m", 8001)
    assert "413" in str(ei.value) and c.sleeps == []


async def test_daily_budget_counts_attempts_and_stops(tmp_path: Path) -> None:
    c = Clock()
    path = tmp_path / "usage.json"
    p = make_pacer(c, tpm=1_000_000, rpd=3, state_path=path)
    for _ in range(3):
        await p.acquire("m", 10)
    assert p.remaining_requests("m") == 0
    with pytest.raises(DailyBudgetExhausted) as ei:
        await p.acquire("m", 10)
    assert "daily request budget" in str(ei.value)
    # another process (new Pacer) sees the same count through the file
    p2 = make_pacer(c, tpm=1_000_000, rpd=3, state_path=path)
    assert p2.requests_today("m") == 3 and p2.remaining_requests("m") == 0
    # the next UTC day starts a fresh budget
    c.utc += timedelta(days=1)
    assert p2.remaining_requests("m") == 3
    await p2.acquire("m", 10)
    assert p2.requests_today("m") == 1


async def test_rate_limit_sets_shared_cooldown() -> None:
    c = Clock()
    p = make_pacer(c, tpm=8000)
    await p.acquire("m", 100)
    info = p.note_rate_limited("m", RateLimited("tpm", {"retry-after": "9"}))
    assert info.retry_after_s == 9 and not info.daily
    await p.acquire("m", 100)
    assert sum(c.sleeps) >= 9


async def test_daily_429_marks_model_exhausted() -> None:
    c = Clock()
    p = make_pacer(c, rpd=1000)
    p.note_rate_limited("m", RateLimited("Rate limit reached: requests per day (RPD)"))
    with pytest.raises(DailyBudgetExhausted):
        await p.acquire("m", 10)
    assert p.remaining_requests("m") == 0


# ── integration with the resilience layer ────────────────────────────────────


@pytest.fixture
def installed_pacer(fake_llm: FakeLLMScript, set_settings: SetSettings) -> Any:
    set_settings(llm_backoff_base_seconds=0.0, llm_max_retries=2, llm_timeout_seconds=5.0)
    providers.breaker.reset()
    c = Clock()
    p = make_pacer(c, tpm=8000, rpd=1000)
    pacer_mod.set_pacer(p)
    yield c, p
    pacer_mod.reset_pacer()
    providers.breaker.reset()


def paced_model(fallback: bool = False) -> ResilientChatModel:
    return ResilientChatModel(
        primary=FakeChatModel(channel="correct"),
        fallback=FakeChatModel(channel="correct") if fallback else None,
        primary_model="m-primary",
        fallback_model="m-fallback" if fallback else "",
        max_tokens=2048,
    )


async def test_calls_reserve_prompt_plus_max_output(
    fake_llm: FakeLLMScript, installed_pacer: Any
) -> None:
    c, p = installed_pacer
    fake_llm.correct("ok", "ok")
    llm = paced_model()
    await llm.ainvoke([{"role": "user", "content": "x" * 350}])  # ~108 + 2048 tokens reserved
    assert p.requests_today("m-primary") == 1
    # a second ~6000-token request cannot start until the bucket has refilled
    await llm.ainvoke([{"role": "user", "content": "x" * 15_000}])
    assert c.sleeps and sum(c.sleeps) > 0


async def test_oversized_request_fails_locally_without_calling_the_model(
    fake_llm: FakeLLMScript, installed_pacer: Any
) -> None:
    fake_llm.correct("never")
    with pytest.raises(RequestTooLarge):
        await paced_model(fallback=True).ainvoke([{"role": "user", "content": "x" * 30_000}])
    assert fake_llm.count("correct") == 0


async def test_429_with_retry_after_backs_off_then_succeeds(
    fake_llm: FakeLLMScript, installed_pacer: Any
) -> None:
    c, p = installed_pacer
    fake_llm.correct(RateLimited("tpm", {"retry-after": "11"}), "answer")
    msg = await paced_model().ainvoke([{"role": "user", "content": "q"}])
    assert msg.content == "answer"
    assert sum(c.sleeps) >= 11  # waited for the server-provided time
    assert p.requests_today("m-primary") == 2  # the 429 attempt counts against the budget


async def test_daily_budget_exhaustion_is_not_retried_and_not_sent_to_the_fallback(
    fake_llm: FakeLLMScript, installed_pacer: Any
) -> None:
    c, p = installed_pacer
    fake_llm.correct("never")
    p.rpd = 0
    with pytest.raises(DailyBudgetExhausted):
        await paced_model(fallback=True).ainvoke([{"role": "user", "content": "q"}])
    assert fake_llm.count("correct") == 0


# ── which model answered ─────────────────────────────────────────────────────


async def test_tokens_are_booked_to_the_model_that_answered(
    fake_llm: FakeLLMScript, set_settings: SetSettings, tmp_path: Path
) -> None:
    set_settings(llm_backoff_base_seconds=0.0, llm_max_retries=1)
    providers.breaker.reset()
    fake_llm.usage = (100, 10)
    cache = DiskCache(tmp_path)
    llm = CachedLLM(
        providers.get_planner_llm(), cache, "planner", "m-primary", fallback_model_id="m-fallback"
    )
    collector = UsageCollector()
    tok = current_usage.set(collector)
    cfg = {"metadata": {"langgraph_node": "correct_sql"}}
    try:
        await llm.ainvoke([{"role": "user", "content": "a"}], cfg)  # type: ignore[arg-type]
        fake_llm.fail("correct", 429, times=2).correct("FB")  # primary: 2 attempts fail
        await llm.ainvoke([{"role": "user", "content": "b"}], cfg)  # type: ignore[arg-type]
        await llm.ainvoke([{"role": "user", "content": "b"}], cfg)  # type: ignore[arg-type]
    finally:
        current_usage.reset(tok)
        providers.breaker.reset()
    legs = [(c["model"], c["leg"], c["cached"]) for c in collector.calls]
    assert legs == [
        ("m-primary", "primary", False),
        ("m-fallback", "fallback", False),
        ("m-fallback", "fallback", True),  # a cache hit replays who answered originally
    ]
    usage = collector.to_dict()["correct_sql"]
    assert usage["m-primary"]["calls"] == 1 and usage["m-fallback"]["calls"] == 2
    assert usage["m-fallback"]["input_tokens"] == 200


async def test_structured_output_marks_the_serving_leg(
    fake_llm: FakeLLMScript, set_settings: SetSettings
) -> None:
    from nl2sql.models import PlanResult
    from tests.fakes import sql_plan

    set_settings(llm_backoff_base_seconds=0.0, llm_max_retries=0)
    providers.breaker.reset()
    fake_llm.fail("plan", 503, times=1).plan(sql_plan("SELECT 1"))
    holder: dict[str, str] = {}
    tok = providers.served_leg.set(holder)
    try:
        llm = providers.get_planner_llm().with_structured_output(PlanResult, method="json_mode")
        res = await llm.ainvoke("q")
    finally:
        providers.served_leg.reset(tok)
        providers.breaker.reset()
    assert res.sql == "SELECT 1" and holder == {"leg": "fallback"}


def test_a_fallback_identical_to_the_primary_is_not_configured(
    set_settings: SetSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_settings(llm_provider="groq", planner_model="p", fast_model="f", fallback_model="f")
    monkeypatch.setattr(providers.settings, "groq_api_key", "k")
    fast = providers.get_fast_llm()
    planner = providers.get_planner_llm()
    assert isinstance(fast, ResilientChatModel) and fast.fallback is None
    assert isinstance(planner, ResilientChatModel) and planner.fallback_model == "f"


def test_retry_after_header_is_read_from_http_errors() -> None:
    err = LLMHTTPError(429, "x")
    assert parse_rate_limit(err).retry_after_s is None  # no response object: defaults apply
