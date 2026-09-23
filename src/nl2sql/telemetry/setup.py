from __future__ import annotations

import structlog

from nl2sql.config import get_settings

settings = get_settings()
log = structlog.get_logger()


def get_langfuse_handler(session_id: str | None = None):
    """
    Return a Langfuse callback handler for LangGraph tracing.
    Returns None if:
      - Langfuse keys are not configured, OR
      - langfuse[langchain] integration is not installed.

    Uses a lazy import so the app boots cleanly even when langfuse
    or its langchain extra is absent from the environment.
    Langfuse v3/v4 uses langfuse.langchain.CallbackHandler
    (previously langfuse.callback.CallbackHandler in v2).
    """
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        log.warning("langfuse_not_configured", msg="Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
        return None

    try:
        from langfuse.langchain import CallbackHandler  # langfuse v3/v4
    except ImportError:
        log.warning(
            "langfuse_langchain_unavailable",
            msg="Install langchain to enable Langfuse tracing: uv add langchain",
        )
        return None

    return CallbackHandler(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        session_id=session_id,
        metadata={"agent_version": settings.agent_version},
    )


def flush_langfuse() -> None:
    """Flush all pending observations — call on app shutdown."""
    try:
        from langfuse import Langfuse
        Langfuse().flush()
    except Exception as exc:
        log.error("langfuse_flush_error", error=str(exc))
