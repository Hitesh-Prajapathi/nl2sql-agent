from __future__ import annotations

import asyncio
import random
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, SecretStr

from nl2sql.config import get_settings
from nl2sql.models import PlanResult
from nl2sql.telemetry import metrics

settings = get_settings()


# ══════════════════════════════════════════════════════════════════════════════
# Fake provider (LLM_PROVIDER=fake) — no network, no API key
# ══════════════════════════════════════════════════════════════════════════════
#
# Every fake LLM instance shares ONE process-wide FakeLLMScript, which holds a
# response queue per *channel*:
#
#   "plan"     structured-output calls on the planner LLM
#              (get_planner_llm().with_structured_output(PlanResult, ...))
#   "correct"  plain-text calls on the planner LLM (SQL correction)
#   "fast"     plain-text calls on the fast LLM (answer synthesis)
#
# Queue items may be: a PlanResult / dict / JSON str (plan channel), a str
# (text channels), an Exception instance (raised), or a callable
# ``fn(messages) -> item`` for dynamic responses.  When a queue is empty the
# LAST scripted item is repeated; if nothing was ever scripted a canned
# default is returned (or RuntimeError if ``strict=True``).

CHANNELS = ("plan", "correct", "fast")


class LLMHTTPError(Exception):
    """A provider HTTP failure (also used by the fake provider to simulate 429/5xx)."""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"LLM provider returned HTTP {status_code}")
        self.status_code = status_code


class LLMUnavailableError(Exception):
    """The circuit breaker is open and no fallback could serve the call."""


CANNED_SQL = "SELECT COUNT(*) AS total FROM customers"


def _canned_plan() -> PlanResult:
    return PlanResult(
        relevant_tables=["customers.customer_id"],
        ambiguity_analysis="None",
        decision="sql",
        sql=CANNED_SQL,
        assumptions=[],
    )


_CANNED: dict[str, Any] = {
    "plan": _canned_plan,
    "correct": lambda: CANNED_SQL,
    "fast": lambda: "Canned summary from the fake LLM.",
}


def _normalise_messages(messages: Any) -> list[dict[str, str]]:
    """Turn any accepted LangChain input into [{'role', 'content'}, ...]."""
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    out: list[dict[str, str]] = []
    for m in messages:
        if isinstance(m, BaseMessage):
            out.append({"role": m.type, "content": str(m.content)})
        elif isinstance(m, dict):
            out.append({"role": str(m.get("role", "")), "content": str(m.get("content", ""))})
        elif isinstance(m, tuple) and len(m) == 2:
            out.append({"role": str(m[0]), "content": str(m[1])})
        else:
            out.append({"role": "user", "content": str(m)})
    return out


@dataclass
class FakeCall:
    channel: str
    messages: list[dict[str, str]]

    @property
    def text(self) -> str:
        """All message contents joined — handy for `assert 'x' in call.text`."""
        return "\n".join(m["content"] for m in self.messages)


@dataclass
class FakeLLMScript:
    """Scriptable, inspectable response source shared by all fake LLMs."""

    usage: tuple[int, int] | None = None  # (input, output) tokens reported by fake LLMs
    latency_ms: float | None = None  # None -> settings.fake_llm_latency_ms
    strict: bool = False  # raise instead of repeating / canned fallback
    calls: list[FakeCall] = field(default_factory=list)
    _queues: dict[str, deque[Any]] = field(default_factory=dict)
    _last: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── scripting ────────────────────────────────────────────────────────────
    def reset(self) -> None:
        with self._lock:
            self.calls.clear()
            self._queues.clear()
            self._last.clear()
            self.latency_ms = None
            self.usage = None
            self.strict = False

    def push(self, channel: str, *items: Any) -> FakeLLMScript:
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel {channel!r}; expected one of {CHANNELS}")
        with self._lock:
            self._queues.setdefault(channel, deque()).extend(items)
        return self

    def plan(self, *items: Any) -> FakeLLMScript:
        return self.push("plan", *items)

    def correct(self, *items: Any) -> FakeLLMScript:
        return self.push("correct", *items)

    def fast(self, *items: Any) -> FakeLLMScript:
        return self.push("fast", *items)

    def fail(self, channel: str, status: int = 429, times: int = 1) -> FakeLLMScript:
        """Queue ``times`` simulated provider HTTP failures (e.g. 429 / 503) on a channel."""
        return self.push(channel, *[LLMHTTPError(status) for _ in range(times)])

    # ── inspection ───────────────────────────────────────────────────────────
    def calls_for(self, channel: str) -> list[FakeCall]:
        return [c for c in self.calls if c.channel == channel]

    def count(self, channel: str) -> int:
        return len(self.calls_for(channel))

    # ── used by the fake models ──────────────────────────────────────────────
    def _effective_latency_s(self) -> float:
        ms = self.latency_ms if self.latency_ms is not None else settings.fake_llm_latency_ms
        return max(ms, 0.0) / 1000.0

    def _next(self, channel: str, messages: Any) -> Any:
        msgs = _normalise_messages(messages)
        with self._lock:
            self.calls.append(FakeCall(channel, msgs))
            queue = self._queues.get(channel)
            if queue:
                item = queue.popleft()
                self._last[channel] = item
            elif channel in self._last and not self.strict:
                item = self._last[channel]
            elif self.strict:
                raise RuntimeError(f"FakeLLMScript: no scripted response left for {channel!r}")
            else:
                item = _CANNED[channel]()
        if callable(item) and not isinstance(item, BaseModel):
            item = item(msgs)
        if isinstance(item, BaseException):
            raise item
        return item

    def respond_text(self, channel: str, messages: Any) -> str:
        item = self._next(channel, messages)
        if isinstance(item, BaseModel):
            return item.model_dump_json()
        return str(item)

    def respond_structured(self, schema: type[BaseModel], messages: Any) -> BaseModel:
        item = self._next("plan", messages)
        if isinstance(item, schema):
            return item
        if isinstance(item, dict):
            return schema.model_validate(item)
        if isinstance(item, BaseModel):
            return schema.model_validate(item.model_dump())
        return schema.model_validate_json(str(item))


_fake_script = FakeLLMScript()


def get_fake_script() -> FakeLLMScript:
    """The process-wide script used by the fake provider."""
    return _fake_script


def reset_fake_script() -> FakeLLMScript:
    _fake_script.reset()
    return _fake_script


def _ai(script: FakeLLMScript, text: str) -> AIMessage:
    if script.usage is None:
        return AIMessage(content=text)
    i, o = script.usage
    return AIMessage(
        content=text, usage_metadata={"input_tokens": i, "output_tokens": o, "total_tokens": i + o}
    )


class FakeChatModel(BaseChatModel):
    """Offline chat model backed by the shared FakeLLMScript."""

    channel: str = "correct"
    script: Any = None

    @property
    def _llm_type(self) -> str:
        return "fake-scripted"

    def _script(self) -> FakeLLMScript:
        return self.script if self.script is not None else _fake_script

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        script = self._script()
        text = script.respond_text(self.channel, messages)
        lat = script._effective_latency_s()
        if lat:
            time.sleep(lat)
        return ChatResult(generations=[ChatGeneration(message=_ai(script, text))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        script = self._script()
        text = script.respond_text(self.channel, messages)
        lat = script._effective_latency_s()
        if lat:
            await asyncio.sleep(lat)
        return ChatResult(generations=[ChatGeneration(message=_ai(script, text))])

    def with_structured_output(
        self,
        schema: Any,
        *,
        method: str = "json_mode",
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        script = self._script()

        def _sync(messages: Any) -> Any:
            result = script.respond_structured(schema, messages)
            lat = script._effective_latency_s()
            if lat:
                time.sleep(lat)
            return result

        async def _async(messages: Any) -> Any:
            result = script.respond_structured(schema, messages)
            lat = script._effective_latency_s()
            if lat:
                await asyncio.sleep(lat)
            return result

        return RunnableLambda(_sync, afunc=_async)


# ══════════════════════════════════════════════════════════════════════════════
# Resilience: timeout, jittered backoff, fallback model, circuit breaker, semaphore
# ══════════════════════════════════════════════════════════════════════════════


def _status_of(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def retry_reason(exc: BaseException) -> str | None:
    """Label for retryable failures (429, 5xx, timeout, connection), else None."""
    if isinstance(exc, LLMUnavailableError):
        return None
    status = _status_of(exc)
    if status == 429:
        return "429"
    if status is not None and status >= 500:
        return "5xx"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"
    return None


class CircuitBreaker:
    """Opens after N consecutive failed calls; allows one trial after the reset window."""

    def __init__(self) -> None:
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= settings.llm_breaker_reset_seconds:
            self.opened_at = time.monotonic()  # half-open: let one trial through
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None
        metrics.LLM_BREAKER_OPEN.set(0)

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= settings.llm_breaker_failure_threshold:
            self.opened_at = time.monotonic()
            metrics.LLM_BREAKER_OPEN.set(1)

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def reset(self) -> None:
        self.success()


breaker = CircuitBreaker()
_sem: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore, int] | None = None


def _semaphore() -> asyncio.Semaphore:
    """Process-wide concurrency cap (recreated per event loop / size change)."""
    global _sem
    loop = asyncio.get_running_loop()
    size = max(1, settings.llm_max_concurrency)
    if _sem is None or _sem[0] is not loop or _sem[2] != size:
        _sem = (loop, asyncio.Semaphore(size), size)
    return _sem[1]


async def _attempts(fn: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``fn`` with timeout and jittered exponential backoff on retryable errors."""
    retries = max(0, settings.llm_max_retries)
    for n in range(retries + 1):
        try:
            async with _semaphore():
                return await asyncio.wait_for(fn(), timeout=settings.llm_timeout_seconds)
        except Exception as exc:
            reason = retry_reason(exc)
            if reason is None or n >= retries:
                raise
            metrics.LLM_RETRIES.labels(reason).inc()
            cap = min(settings.llm_backoff_max_seconds, settings.llm_backoff_base_seconds * 2**n)
            await asyncio.sleep(random.uniform(0, cap))  # noqa: S311 - jitter, not crypto
    raise AssertionError("unreachable")  # pragma: no cover


async def resilient_call(
    primary: Callable[[], Awaitable[Any]],
    fallback: Callable[[], Awaitable[Any]] | None = None,
) -> Any:
    """primary (retried, breaker-guarded) -> fallback model on retryable failure."""
    primary_exc: BaseException | None = None
    if breaker.allow():
        try:
            result = await _attempts(primary)
            breaker.success()
            return result
        except Exception as exc:
            if retry_reason(exc) is None:
                raise  # non-transient (bad request, parse error): not the provider's health
            breaker.failure()
            primary_exc = exc
    if fallback is None:
        if primary_exc is not None:
            raise primary_exc
        raise LLMUnavailableError("LLM circuit breaker is open")
    metrics.LLM_FALLBACKS.inc()
    return await _attempts(fallback)


class ResilientChatModel(BaseChatModel):
    """Wraps a primary and an optional fallback chat model with the resilience policy."""

    primary: Any
    fallback: Any = None

    @property
    def _llm_type(self) -> str:
        return "resilient"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        result: ChatResult = self.primary._generate(messages, stop=stop, **kwargs)
        return result

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        def call(model: Any) -> Callable[[], Awaitable[Any]]:
            async def _c() -> Any:
                return await model._agenerate(messages, stop=stop, **kwargs)

            return _c

        fb = call(self.fallback) if self.fallback is not None else None
        result: ChatResult = await resilient_call(call(self.primary), fb)
        return result

    def with_structured_output(
        self,
        schema: Any,
        *,
        method: str = "json_mode",
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        def build(model: Any) -> Runnable[Any, Any]:
            r: Runnable[Any, Any] = model.with_structured_output(
                schema, method=method, include_raw=include_raw, **kwargs
            )
            return r

        p = build(self.primary)
        f = build(self.fallback) if self.fallback is not None else None

        async def _run(messages: Any, config: Any = None) -> Any:
            fb = (lambda: f.ainvoke(messages, config)) if f is not None else None
            return await resilient_call(lambda: p.ainvoke(messages, config), fb)

        return RunnableLambda(_run)


# ══════════════════════════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════════════════════════


def _groq(model: str, max_tokens: int) -> BaseChatModel:
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=model,
        temperature=0,
        max_tokens=max_tokens,
        api_key=SecretStr(settings.groq_api_key),
        max_retries=0,  # retries are owned by resilient_call
    )


def get_planner_llm() -> BaseChatModel:
    """70B — planning, SQL generation, error correction."""
    if settings.llm_provider == "fake":
        return ResilientChatModel(
            primary=FakeChatModel(channel="correct"),
            fallback=FakeChatModel(channel="correct"),
        )
    fb = _groq(settings.fallback_model, 2048) if settings.fallback_model else None
    return ResilientChatModel(primary=_groq(settings.planner_model, 2048), fallback=fb)


def get_fast_llm() -> BaseChatModel:
    """8B — synthesis, classification (very fast, generous rate limits)."""
    if settings.llm_provider == "fake":
        return ResilientChatModel(
            primary=FakeChatModel(channel="fast"),
            fallback=FakeChatModel(channel="fast"),
        )
    fb = _groq(settings.fallback_model, 1024) if settings.fallback_model else None
    return ResilientChatModel(primary=_groq(settings.fast_model, 1024), fallback=fb)
