from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nl2sql.llm import providers
from nl2sql.llm.providers import (
    FakeLLMScript,
    LLMHTTPError,
    LLMUnavailableError,
    get_planner_llm,
    resilient_call,
)
from nl2sql.models import PlanResult
from tests.fakes import SetSettings, sql_plan


@pytest.fixture(autouse=True)
def _fast(fake_llm: FakeLLMScript, set_settings: SetSettings) -> Any:
    set_settings(
        llm_backoff_base_seconds=0.0,
        llm_max_retries=2,
        llm_timeout_seconds=5.0,
        llm_breaker_failure_threshold=3,
        llm_breaker_reset_seconds=60.0,
    )
    providers.breaker.reset()
    yield
    providers.breaker.reset()


async def test_retry_then_success(fake_llm: FakeLLMScript) -> None:
    fake_llm.fail("correct", 429, times=2).correct("SELECT 1")
    msg = await get_planner_llm().ainvoke("q")
    assert msg.content == "SELECT 1"
    assert fake_llm.count("correct") == 3


async def test_structured_retry_on_503(fake_llm: FakeLLMScript) -> None:
    fake_llm.fail("plan", 503, times=1).plan(sql_plan("SELECT 1"))
    llm = get_planner_llm().with_structured_output(PlanResult, method="json_mode")
    res = await llm.ainvoke("q")
    assert isinstance(res, PlanResult) and res.sql == "SELECT 1"


async def test_fallback_after_retries_exhausted(fake_llm: FakeLLMScript) -> None:
    fake_llm.fail("correct", 429, times=3).correct("FALLBACK")
    msg = await get_planner_llm().ainvoke("q")
    assert msg.content == "FALLBACK"


async def test_non_retryable_error_propagates_once(fake_llm: FakeLLMScript) -> None:
    fake_llm.correct(LLMHTTPError(400))
    with pytest.raises(LLMHTTPError):
        await get_planner_llm().ainvoke("q")
    assert fake_llm.count("correct") == 1
    assert providers.breaker.failures == 0


async def test_breaker_opens_and_skips_primary() -> None:
    calls = 0

    async def bad() -> Any:
        nonlocal calls
        calls += 1
        raise LLMHTTPError(500)

    for _ in range(3):
        with pytest.raises(LLMHTTPError):
            await resilient_call(bad)
    assert providers.breaker.is_open
    before = calls
    with pytest.raises(LLMUnavailableError):
        await resilient_call(bad)
    assert calls == before  # primary not touched while open


async def test_breaker_open_uses_fallback() -> None:
    providers.breaker.opened_at = 10**12  # far future -> stays open

    async def ok() -> str:
        return "fb"

    async def bad() -> str:
        raise AssertionError("primary must not run")

    assert await resilient_call(bad, ok) == "fb"


async def test_timeout_is_retryable(set_settings: SetSettings) -> None:
    set_settings(llm_timeout_seconds=0.01, llm_max_retries=1)
    n = 0

    async def slow() -> str:
        nonlocal n
        n += 1
        await asyncio.sleep(1)
        return "x"

    with pytest.raises(TimeoutError):
        await resilient_call(slow)
    assert n == 2


async def test_concurrency_semaphore(set_settings: SetSettings) -> None:
    set_settings(llm_max_concurrency=2)
    running = peak = 0

    async def work() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    await asyncio.gather(*[resilient_call(work) for _ in range(6)])
    assert peak == 2


async def test_fake_usage_metadata(fake_llm: FakeLLMScript) -> None:
    fake_llm.usage = (10, 5)
    msg = await get_planner_llm().ainvoke("q")
    assert msg.usage_metadata is not None
    assert msg.usage_metadata["input_tokens"] == 10
