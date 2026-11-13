from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel

from nl2sql.config import get_settings
from nl2sql.models import PlanResult

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

    latency_ms: float | None = None  # None -> settings.fake_llm_latency_ms
    strict: bool = False             # raise instead of repeating / canned fallback
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
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: Any = None, **kwargs: Any,
    ) -> ChatResult:
        script = self._script()
        text = script.respond_text(self.channel, messages)
        lat = script._effective_latency_s()
        if lat:
            time.sleep(lat)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: Any = None, **kwargs: Any,
    ) -> ChatResult:
        script = self._script()
        text = script.respond_text(self.channel, messages)
        lat = script._effective_latency_s()
        if lat:
            await asyncio.sleep(lat)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def with_structured_output(  # type: ignore[override]
        self, schema: Any, *, method: str = "json_mode", include_raw: bool = False, **kwargs: Any,
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
# Factory
# ══════════════════════════════════════════════════════════════════════════════

def get_planner_llm() -> BaseChatModel:
    """70B — planning, SQL generation, error correction."""
    if settings.llm_provider == "fake":
        return FakeChatModel(channel="correct")
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=settings.planner_model,
        temperature=0,
        max_tokens=2048,
        api_key=settings.groq_api_key,
    )


def get_fast_llm() -> BaseChatModel:
    """8B — synthesis, classification (very fast, generous rate limits)."""
    if settings.llm_provider == "fake":
        return FakeChatModel(channel="fast")
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=settings.fast_model,
        temperature=0,
        max_tokens=1024,
        api_key=settings.groq_api_key,
    )
