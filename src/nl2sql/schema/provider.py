from __future__ import annotations

import time
from abc import ABC, abstractmethod

import asyncpg
import structlog

from nl2sql.config import get_settings
from nl2sql.schema.introspect import get_catalog_hash, introspect_schema

log = structlog.get_logger()


class SchemaProvider(ABC):
    """Interface — swap FullSchemaProvider for RetrievalSchemaProvider for 200+ tables."""

    @abstractmethod
    async def get_context(self, question: str = "") -> str: ...


class FullSchemaProvider(SchemaProvider):
    """
    Returns complete schema context for all tables.
    Suitable for ≤ ~20 tables.
    Cache is invalidated by a hash of pg_catalog — change-based, not time-based.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._cache: str | None = None
        self._cache_hash: str | None = None
        self._built_at: float = 0.0

    async def get_context(self, question: str = "") -> str:
        current_hash = await get_catalog_hash(self._pool)
        settings = get_settings()
        is_expired = (time.time() - self._built_at) > settings.schema_cache_ttl_seconds

        if self._cache and self._cache_hash == current_hash and not is_expired:
            return self._cache

        reason = "ttl_expired" if (self._cache and is_expired) else "catalog_changed_or_cold"
        log.info("schema_cache_miss", reason=reason)
        t0 = time.perf_counter()
        context = await introspect_schema(self._pool)
        elapsed = (time.perf_counter() - t0) * 1000
        log.info("schema_introspected", duration_ms=round(elapsed))

        self._cache = context
        self._cache_hash = current_hash
        self._built_at = time.time()
        return context

    def invalidate(self) -> None:
        self._cache = None
        self._cache_hash = None
        self._built_at = 0.0
