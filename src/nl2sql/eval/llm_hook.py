"""Evaluation LLM hook: on-disk response cache + per-node token accounting.

The cache key covers (model id, call kind incl. structured-output schema, sampling params, full
message list), so a changed prompt, schema or model never reuses an old answer. Hits replay the
stored response AND its token usage (flagged ``cached``) so cost is never silently zero.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import LLMResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.runnables.config import RunnableConfig, merge_configs
from pydantic import BaseModel

# ── Token accounting ──────────────────────────────────────────────────────────


@dataclass
class UsageBucket:
    calls: int = 0
    cached_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: UsageBucket) -> None:
        self.calls += other.calls
        self.cached_calls += other.cached_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@dataclass
class UsageCollector:
    """Per-question usage keyed by (graph node, model id)."""

    buckets: dict[tuple[str, str], UsageBucket] = field(default_factory=lambda: defaultdict(UsageBucket))

    def record(
        self, node: str, model_id: str, input_tokens: int, output_tokens: int, cached: bool
    ) -> None:
        b = self.buckets[(node, model_id)]
        b.calls += 1
        b.cached_calls += int(cached)
        b.input_tokens += input_tokens
        b.output_tokens += output_tokens

    def to_dict(self) -> dict[str, dict[str, dict[str, int]]]:
        out: dict[str, dict[str, dict[str, int]]] = {}
        for (node, model), b in sorted(self.buckets.items()):
            out.setdefault(node, {})[model] = {
                "calls": b.calls,
                "cached_calls": b.cached_calls,
                "input_tokens": b.input_tokens,
                "output_tokens": b.output_tokens,
            }
        return out


current_usage: ContextVar[UsageCollector | None] = ContextVar("nl2sql_eval_usage", default=None)


class UsageCapture(BaseCallbackHandler):
    """Collects ``usage_metadata`` from LLM results produced inside one wrapped call."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for gens in response.generations:
            for g in gens:
                msg = getattr(g, "message", None)
                usage = getattr(msg, "usage_metadata", None)
                if usage:
                    self.input_tokens += int(usage.get("input_tokens", 0))
                    self.output_tokens += int(usage.get("output_tokens", 0))


# ── Disk cache ────────────────────────────────────────────────────────────────


class DiskCache:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            value = cast("dict[str, Any]", json.loads(self._path(key).read_text()))
        except (OSError, ValueError):
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(value, f)
            os.replace(tmp, path)  # atomic: concurrent writers cannot leave a torn file
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def normalise_messages(messages: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    for m in messages:
        if isinstance(m, BaseMessage):
            out.append({"role": m.type, "content": str(m.content)})
        elif isinstance(m, dict):
            out.append({"role": str(m.get("role", "")), "content": str(m.get("content", ""))})
        else:
            out.append({"role": "user", "content": str(m)})
    return out


def _node_of(config: RunnableConfig | None) -> str:
    meta = (config or {}).get("metadata") or {}
    return str(meta.get("langgraph_node") or "direct")


class CachedLLM:
    """Wraps a chat model: ``ainvoke`` and ``with_structured_output`` with cache + accounting."""

    def __init__(
        self, llm: BaseChatModel, cache: DiskCache | None, role: str, model_id: str
    ) -> None:
        self._llm = llm
        self._cache = cache
        self._role = role
        self._model_id = model_id

    def _key(self, kind: str, messages: list[dict[str, str]]) -> str:
        return DiskCache.make_key(
            {
                "model": self._model_id,
                "role": self._role,
                "kind": kind,
                "params": {"temperature": 0},
                "messages": messages,
            }
        )

    def _report(self, config: RunnableConfig | None, i: int, o: int, cached: bool) -> None:
        collector = current_usage.get()
        if collector is not None:
            collector.record(_node_of(config), self._model_id, i, o, cached)

    async def ainvoke(self, messages: Any, config: RunnableConfig | None = None) -> AIMessage:
        norm = normalise_messages(messages)
        key = self._key("text", norm)
        if self._cache is not None and (hit := self._cache.get(key)) is not None:
            u = hit.get("usage", {})
            self._report(config, u.get("input", 0), u.get("output", 0), True)
            return AIMessage(
                content=hit["content"],
                usage_metadata={
                    "input_tokens": u.get("input", 0),
                    "output_tokens": u.get("output", 0),
                    "total_tokens": u.get("input", 0) + u.get("output", 0),
                },
                response_metadata={"nl2sql_cache_hit": True},
            )
        capture = UsageCapture()
        cfg = merge_configs(config, {"callbacks": [capture]})
        resp = await self._llm.ainvoke(messages, cfg)
        content = str(resp.content)
        self._report(config, capture.input_tokens, capture.output_tokens, False)
        if self._cache is not None:
            self._cache.put(
                key,
                {"content": content, "usage": {"input": capture.input_tokens, "output": capture.output_tokens}},
            )
        return cast(AIMessage, resp)

    def with_structured_output(
        self, schema: type[BaseModel], *, method: str = "json_mode", **kwargs: Any
    ) -> Runnable[Any, Any]:
        inner = self._llm.with_structured_output(schema, method=method, **kwargs)
        kind = f"struct:{schema.__name__}:{method}"

        async def _run(messages: Any, config: RunnableConfig | None = None) -> Any:
            norm = normalise_messages(messages)
            key = self._key(kind, norm)
            if self._cache is not None and (hit := self._cache.get(key)) is not None:
                u = hit.get("usage", {})
                self._report(config, u.get("input", 0), u.get("output", 0), True)
                return schema.model_validate_json(hit["json"])
            capture = UsageCapture()
            cfg = merge_configs(config, {"callbacks": [capture]})
            result = await inner.ainvoke(messages, cfg)
            self._report(config, capture.input_tokens, capture.output_tokens, False)
            if self._cache is not None and isinstance(result, BaseModel):
                self._cache.put(
                    key,
                    {
                        "json": result.model_dump_json(),
                        "usage": {"input": capture.input_tokens, "output": capture.output_tokens},
                    },
                )
            return result

        return RunnableLambda(_run)


class EvalLLMHook:
    """Installed in ``nl2sql.agent.llm.llm_hook`` for the duration of an evaluation task."""

    def __init__(self, cache: DiskCache | None) -> None:
        self.cache = cache

    def wrap(self, llm: BaseChatModel, role: str, model_id: str) -> CachedLLM:
        return CachedLLM(llm, self.cache, role, model_id)
