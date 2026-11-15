"""Token-bucket pacer, daily request budget, 429 handling and model attribution (offline)."""

from __future__ import annotations

import json
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
    # Only an explicit per-day phrase makes a 429 daily; long waits and "daily" alone do not.
    assert not huge.daily
    assert not parse_rate_limit(RateLimited("daily traffic is high")).daily
    for phrase in ("tokens per day", "(TPD)", "requests per day", "(RPD)", "per day"):
        assert parse_rate_limit(RateLimited(f"Rate limit reached: {phrase}")).daily
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


TPM_MSG = "Rate limit reached for model on tokens per minute (TPM): Limit 8000"
TPD_MSG = "Rate limit reached for model on tokens per day (TPD): Limit 200000, Used 199900"


def test_tpm_429_with_long_reset_requests_header_is_not_daily() -> None:
    c = Clock()
    p = make_pacer(c, rpd=1000)
    info = p.note_rate_limited(
        "m",
        RateLimited(TPM_MSG, {"retry-after": "7", "x-ratelimit-reset-requests": "23h59m0s"}),
    )
    assert not info.daily
    assert p.remaining_requests("m") == 1000 and p.daily_strikes("m") == 0


async def test_single_daily_429_backs_off_and_does_not_exhaust() -> None:
    c = Clock()
    p = make_pacer(c, rpd=1000)
    info = p.note_rate_limited("m", RateLimited(TPD_MSG))
    assert info.daily and p.daily_strikes("m") == 1
    assert p.remaining_requests("m") == 1000  # not exhausted by one 429
    await p.acquire("m", 10)
    assert sum(c.sleeps) >= 300  # first step of the 5/10/20/30 min schedule


async def test_daily_429_honours_retry_after_then_escalates() -> None:
    c = Clock()
    p = make_pacer(c, rpd=1000)
    p.note_rate_limited("m", RateLimited(TPD_MSG, {"retry-after": "42"}))
    await p.acquire("m", 10)
    assert 42 <= sum(c.sleeps) < 60
    for want in (600, 1200, 1800):
        p.note_rate_limited("m", RateLimited(TPD_MSG))
        before = c.t
        await p.acquire("m", 10)
        assert want <= c.t - before < want + 5


async def test_daily_429s_exhaust_after_span_and_success_resets() -> None:
    c = Clock()
    p = make_pacer(c, rpd=1000)
    for _ in range(4):  # 4 strikes within ~35 min: below an hour, keep backing off
        p.note_rate_limited("m", RateLimited(TPD_MSG))
        await p.acquire("m", 10)
    assert p.remaining_requests("m") > 0 and p.daily_strikes("m") == 4
    p.settle("m", 10, 10)  # a success resets the counter
    assert p.daily_strikes("m") == 0
    for _ in range(4):
        p.note_rate_limited("m", RateLimited(TPD_MSG))
        await p.acquire("m", 10)
    p.note_rate_limited("m", RateLimited(TPD_MSG))  # 5th: >= 4 strikes spanning > 1 h
    with pytest.raises(DailyBudgetExhausted):
        await p.acquire("m", 10)


async def test_n_daily_429s_stop_when_no_span_is_required() -> None:
    c = Clock()
    p = make_pacer(c, daily_max_strikes=4, daily_min_span_s=0)
    for _ in range(3):
        p.note_rate_limited("m", RateLimited(TPD_MSG))
        await p.acquire("m", 10)
    p.note_rate_limited("m", RateLimited(TPD_MSG))
    with pytest.raises(DailyBudgetExhausted):
        await p.acquire("m", 10)


def test_429_is_logged_and_last_message_persisted(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    c = Clock()
    path = tmp_path / "u.json"
    p = make_pacer(c, state_path=path)
    msg = "Rate limit gsk_abcdefgh12345678 " + "x" * 800
    hdrs = {"retry-after": "3", "x-ratelimit-limit-tokens": "8000", "a": "b"}
    with capture_logs() as logs:
        p.note_rate_limited("m", RateLimited(msg, hdrs))
    ev = next(e for e in logs if e["event"] == "llm_rate_limited")
    assert ev["log_level"] == "warning" and ev["model"] == "m"
    assert ev["headers"] == {"retry-after": "3", "x-ratelimit-limit-tokens": "8000"}
    assert len(ev["message"]) <= 500 and "gsk_abcdefgh" not in ev["message"]
    entry = json.loads(path.read_text())["models"]["m"]
    assert entry["last_429_message"] == ev["message"]


def test_stale_exhausted_state_is_cleared_on_restart(tmp_path: Path) -> None:
    c = Clock()
    path = tmp_path / "u.json"
    old = (c.now() - timedelta(hours=3)).isoformat()
    base = {"requests": 5, "rate_limited": 1, "exhausted": True}
    models = {
        "legacy": {"requests": 62, "rate_limited": 1, "exhausted": True},  # no timestamp
        "stale": {**base, "exhausted_at": old},
        "fresh": {**base, "exhausted_at": c.now().isoformat()},
    }
    path.write_text(json.dumps({"date": c.now().strftime("%Y-%m-%d"), "models": models}))
    p = make_pacer(c, state_path=path)
    assert p.remaining_requests("legacy") > 0 and p.remaining_requests("stale") > 0
    assert p.remaining_requests("fresh") == 0  # recent: still honoured


def test_reset_exhausted_clears_todays_flag(tmp_path: Path) -> None:
    c = Clock()
    path = tmp_path / "u.json"
    p = make_pacer(c, state_path=path, daily_max_strikes=1, daily_min_span_s=0)
    p.note_rate_limited("m", RateLimited(TPD_MSG))
    assert p.remaining_requests("m") == 0
    assert make_pacer(c, state_path=path).remaining_requests("m") == 0  # fresh flag survives
    assert make_pacer(c, state_path=path).reset_exhausted() == ["m"]
    assert make_pacer(c, state_path=path).remaining_requests("m") > 0


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


# ── wall-clock sleeps (laptop suspend) ───────────────────────────────────────────────


class SuspendingWall:
    """Wall clock plus a sleep that 'suspends' once: wall time jumps while no sleep time passes."""

    def __init__(self, jump_after_calls: int = 0, jump_s: float = 0.0) -> None:
        self.t = 1_000_000.0
        self.calls: list[float] = []
        self.jump_after_calls = jump_after_calls
        self.jump_s = jump_s

    def wall(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.calls.append(s)
        self.t += s
        if len(self.calls) == self.jump_after_calls:
            self.t += self.jump_s  # the machine slept for jump_s; the sleep call never knew


async def test_wall_sleep_short_wait_is_one_plain_sleep() -> None:
    w = SuspendingWall()
    await pacer_mod.wall_sleep(12.5, sleep=w.sleep, wall=w.wall)
    assert w.calls == [12.5]


async def test_wall_sleep_slices_long_waits_into_30s_chunks() -> None:
    w = SuspendingWall()
    start = w.t
    await pacer_mod.wall_sleep(100, sleep=w.sleep, wall=w.wall)
    assert w.calls == [30.0, 30.0, 30.0, pytest.approx(10.0)]
    assert w.t - start == pytest.approx(100)


async def test_wall_sleep_ends_at_wall_deadline_after_suspend() -> None:
    # A 23-minute wait; the machine sleeps for 20 minutes after the 2nd chunk. The wait must end
    # at the original wall deadline (23 min after the start), not 20 minutes later.
    w = SuspendingWall(jump_after_calls=2, jump_s=20 * 60)
    start = w.t
    await pacer_mod.wall_sleep(23 * 60, sleep=w.sleep, wall=w.wall)
    assert w.t - start == pytest.approx(23 * 60, abs=1)
    assert len(w.calls) == 2 + 4  # two chunks, the suspend, then the last 2 min in 30 s chunks
    assert max(w.calls) <= 30.0


async def test_pacer_cooldown_is_a_wall_clock_deadline() -> None:
    w = SuspendingWall()
    mono = [0.0]  # monotonic clock that does not advance while the "machine" sleeps

    def clock() -> float:
        return mono[0]

    async def sleep(s: float) -> None:
        await w.sleep(s)
        mono[0] += s
        if len(w.calls) == 3:
            w.t += 500.0  # 500 s of suspend: wall advances, monotonic does not

    p = Pacer(
        clock=clock,
        wall=w.wall,
        utcnow=lambda: datetime(2026, 11, 14, tzinfo=UTC),
        sleep=sleep,
        rpd_margin=0,
    )
    start = w.t
    p.note_rate_limited("m", RateLimited("rate limit", {"retry-after": "800"}))
    await p.acquire("m", 100)
    assert 800 <= w.t - start < 840  # ends at the deadline, not ~500 s later
    assert max(w.calls) <= 30.0
