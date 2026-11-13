from __future__ import annotations

import os
from typing import Any

import structlog

from nl2sql.config import get_settings

settings = get_settings()
log = structlog.get_logger()

_client_initialized = False


def _sync_env_vars() -> None:
    if settings.langfuse_public_key and "LANGFUSE_PUBLIC_KEY" not in os.environ:
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
    if settings.langfuse_secret_key and "LANGFUSE_SECRET_KEY" not in os.environ:
        os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
    if settings.langfuse_host and "LANGFUSE_HOST" not in os.environ:
        os.environ["LANGFUSE_HOST"] = settings.langfuse_host


def get_langfuse_handler(session_id: str | None = None) -> Any | None:
    """
    Return a Langfuse callback handler for LangGraph tracing.
    Returns None if:
      - Langfuse keys are not configured, OR
      - langfuse[langchain] integration is not installed.
    """
    global _client_initialized
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        log.warning(
            "langfuse_not_configured", msg="Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY"
        )
        return None

    _sync_env_vars()

    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler
    except ImportError:
        log.warning(
            "langfuse_langchain_unavailable",
            msg="Install langchain to enable Langfuse tracing: uv add langchain",
        )
        return None

    try:
        if not _client_initialized:
            Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
            _client_initialized = True

        return CallbackHandler(public_key=settings.langfuse_public_key)
    except Exception as exc:
        log.error("langfuse_init_error", error=str(exc))
        return None


def flush_langfuse() -> None:
    """Flush all pending observations — call on app shutdown."""
    _sync_env_vars()
    try:
        from langfuse import Langfuse

        Langfuse().flush()
    except Exception as exc:
        log.error("langfuse_flush_error", error=str(exc))
