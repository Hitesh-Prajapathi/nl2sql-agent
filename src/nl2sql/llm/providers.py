from __future__ import annotations

from langchain_groq import ChatGroq
from nl2sql.config import get_settings

settings = get_settings()


def get_planner_llm() -> ChatGroq:
    """70B — planning, SQL generation, error correction."""
    return ChatGroq(
        model=settings.planner_model,
        temperature=0,
        max_tokens=2048,
        api_key=settings.groq_api_key,
    )


def get_fast_llm() -> ChatGroq:
    """8B — synthesis, classification (very fast, generous rate limits)."""
    return ChatGroq(
        model=settings.fast_model,
        temperature=0,
        max_tokens=1024,
        api_key=settings.groq_api_key,
    )
