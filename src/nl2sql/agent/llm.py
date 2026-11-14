"""LLM access for graph nodes, with an optional evaluation hook.

Nodes call ``planner_llm()`` / ``fast_llm()`` instead of the provider factories directly. In normal
operation these return the provider model untouched. The evaluation harness installs an
``LLMHook`` (a ContextVar, so concurrent questions stay isolated) that wraps the model with an
on-disk response cache and per-node token accounting.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from nl2sql.config import get_settings
from nl2sql.llm import providers


class LLMHook(Protocol):
    def wrap(
        self, llm: BaseChatModel, role: str, model_id: str, fallback_model_id: str = ""
    ) -> Any: ...


llm_hook: ContextVar[LLMHook | None] = ContextVar("nl2sql_llm_hook", default=None)


def planner_llm() -> Any:
    """Planner model: planning, SQL generation, error correction."""
    llm = providers.get_planner_llm()
    hook = llm_hook.get()
    if hook is None:
        return llm
    return hook.wrap(llm, "planner", _model_id("planner"), _fallback_id("planner"))


def fast_llm() -> Any:
    """Fast model: answer synthesis."""
    llm = providers.get_fast_llm()
    hook = llm_hook.get()
    if hook is None:
        return llm
    return hook.wrap(llm, "fast", _model_id("fast"), _fallback_id("fast"))


def _model_id(role: str) -> str:
    s = get_settings()
    if s.llm_provider == "fake":
        return f"fake-{role}"
    return s.planner_model if role == "planner" else s.fast_model


def _fallback_id(role: str) -> str:
    s = get_settings()
    if s.llm_provider == "fake":
        return f"fake-{role}"
    primary = _model_id(role)
    # A fallback equal to the primary is not used (see providers._resilient_groq)
    return s.fallback_model if s.fallback_model and s.fallback_model != primary else primary
