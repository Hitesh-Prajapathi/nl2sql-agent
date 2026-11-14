"""Multi-database registry: ``db_id`` -> (pool, schema provider, profile).

The graph never binds a pool at build time; nodes call ``registry.get(db_id)`` per request.

Pool strategy (decision): ONE LAZY POOL PER DATABASE (``min_size=0``, small ``max_size``), each
connecting as a role that is read-only on exactly one schema with ``search_path`` pinned
(``scripts/bird_setup.py``). The alternative, one pool plus ``SET LOCAL search_path`` per
transaction, would let a single role read every schema (and generated SQL could itself switch
search_path), which breaks the per-database isolation the safety model relies on. The cost is
more server connections, bounded by ``registry_pool_max_size`` x databases in use; keep
(runner concurrency x pools) well below the server's ``max_connections``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import structlog

from nl2sql.config import get_settings
from nl2sql.db.connection import create_pool, get_pool
from nl2sql.schema.profile import DatabaseProfile
from nl2sql.schema.provider import FullSchemaProvider, SchemaProvider
from nl2sql.schema.store import StoredProfileSchemaProvider, profile_path

log = structlog.get_logger()

DEFAULT_DB_ID = "default"


class UnknownDatabaseError(KeyError):
    """Raised when a request names a db_id that is not registered."""


@dataclass
class DatabaseResources:
    db_id: str
    pool: asyncpg.Pool | None
    schema_provider: SchemaProvider

    @property
    def profile(self) -> DatabaseProfile | None:
        prof = getattr(self.schema_provider, "profile", None)
        return prof if isinstance(prof, DatabaseProfile) else None


PoolFactory = Callable[[], Awaitable[asyncpg.Pool]]


@dataclass
class _Entry:
    db_id: str
    pool_factory: PoolFactory | None = None
    pool: asyncpg.Pool | None = None
    schema_provider: SchemaProvider | None = None
    stored_profile_file: Path | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    resources: DatabaseResources | None = None


class DatabaseRegistry:
    def __init__(self, default_db_id: str = DEFAULT_DB_ID) -> None:
        self.default_db_id = default_db_id
        self._entries: dict[str, _Entry] = {}

    # ── registration ──────────────────────────────────────────────────────────

    def register(
        self,
        db_id: str,
        *,
        pool: asyncpg.Pool | None = None,
        pool_factory: PoolFactory | None = None,
        schema_provider: SchemaProvider | None = None,
        stored_profile_file: str | Path | None = None,
    ) -> None:
        self._entries[db_id] = _Entry(
            db_id=db_id,
            pool=pool,
            pool_factory=pool_factory,
            schema_provider=schema_provider,
            stored_profile_file=Path(stored_profile_file) if stored_profile_file else None,
        )

    def register_dsn(
        self,
        db_id: str,
        dsn: str,
        *,
        search_path: str | None = None,
        max_size: int | None = None,
        statement_timeout_ms: int | None = None,
        use_stored_profile: bool = True,
        profile_dir: str | Path | None = None,
    ) -> None:
        """Register a lazily-created pool for ``dsn`` (created on first use)."""
        size = max_size or get_settings().registry_pool_max_size

        async def _factory() -> asyncpg.Pool:
            return await create_pool(
                dsn,
                min_size=0,
                max_size=size,
                statement_timeout_ms=statement_timeout_ms,
                search_path=search_path,
            )

        stored = profile_path(db_id, profile_dir) if use_stored_profile else None
        self.register(
            db_id,
            pool_factory=_factory,
            stored_profile_file=stored if stored and stored.exists() else None,
        )

    @classmethod
    def from_single(
        cls,
        pool: asyncpg.Pool | None,
        schema_provider: SchemaProvider | None = None,
        db_id: str = DEFAULT_DB_ID,
    ) -> DatabaseRegistry:
        """Back-compat: one already-built pool (and optional provider) under ``db_id``."""
        reg = cls(default_db_id=db_id)
        reg.register(db_id, pool=pool, schema_provider=schema_provider)
        return reg

    @classmethod
    def from_default(cls) -> DatabaseRegistry:
        """The default database from DATABASE_URL, using the process-wide pool."""
        reg = cls()
        reg.register(DEFAULT_DB_ID, pool_factory=get_pool)
        return reg

    @classmethod
    def for_bird(
        cls,
        db_ids: list[str],
        *,
        profile_dir: str | Path | None = None,
        statement_timeout_ms: int | None = None,
        max_size: int | None = None,
        url_template: str | None = None,
        use_stored_profiles: bool = True,
    ) -> DatabaseRegistry:
        s = get_settings()
        template = url_template or s.bird_database_url_template
        reg = cls(default_db_id=db_ids[0] if db_ids else DEFAULT_DB_ID)
        for db_id in db_ids:
            reg.register_dsn(
                db_id,
                template.format(db_id=db_id),
                search_path=db_id,
                max_size=max_size,
                statement_timeout_ms=statement_timeout_ms or s.eval_statement_timeout_ms,
                profile_dir=profile_dir,
                use_stored_profile=use_stored_profiles,
            )
        return reg

    # ── lookup ────────────────────────────────────────────────────────────────

    def known_ids(self) -> list[str]:
        return sorted(self._entries)

    def has(self, db_id: str) -> bool:
        return db_id in self._entries

    async def get(self, db_id: str | None = None) -> DatabaseResources:
        key = db_id or self.default_db_id
        entry = self._entries.get(key)
        if entry is None:
            raise UnknownDatabaseError(
                f"Unknown database {key!r}. Known databases: {', '.join(self.known_ids()) or '-'}"
            )
        if entry.resources is not None:
            return entry.resources
        async with entry.lock:
            if entry.resources is None:
                entry.resources = await self._build(entry)
        return entry.resources

    async def pool_for(self, db_id: str | None = None) -> asyncpg.Pool:
        res = await self.get(db_id)
        if res.pool is None:
            raise RuntimeError(f"Database {res.db_id!r} has no connection pool")
        return res.pool

    async def _build(self, entry: _Entry) -> DatabaseResources:
        pool = entry.pool
        provider = entry.schema_provider
        stored = entry.stored_profile_file
        if provider is None and stored is not None:
            provider = StoredProfileSchemaProvider.from_file(stored)
            log.info("registry_stored_profile", db_id=entry.db_id, file=str(stored))
        if pool is None and entry.pool_factory is not None:
            pool = await entry.pool_factory()
        if provider is None:
            if pool is None:
                raise RuntimeError(f"Database {entry.db_id!r} has neither pool nor schema provider")
            provider = FullSchemaProvider(pool)
        return DatabaseResources(db_id=entry.db_id, pool=pool, schema_provider=provider)

    async def close(self) -> None:
        """Close pools this registry created (pools passed in by the caller are left alone)."""
        for entry in self._entries.values():
            res = entry.resources
            if res is not None and res.pool is not None and entry.pool_factory is not None:
                if entry.pool_factory is get_pool:
                    continue  # the process-wide pool is closed by connection.close_pool()
                with contextlib.suppress(Exception):
                    await res.pool.close()
            entry.resources = None
