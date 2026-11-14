"""Bounded session store behind an interface.

``InMemorySessionStore`` is per-process: with several uvicorn workers each worker has
its own store, so follow-ups are only correct with sticky routing. Implement
``SessionStore`` over Redis/Postgres for shared sessions.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol


class SessionStore(Protocol):
    async def get(self, session_id: str) -> list[dict[str, Any]]: ...

    async def append(self, session_id: str, turn: dict[str, Any]) -> None: ...


def _size(turns: list[dict[str, Any]]) -> int:
    return len(json.dumps(turns, default=str).encode())


class InMemorySessionStore:
    def __init__(
        self,
        ttl_seconds: float,
        max_turns: int,
        max_bytes: int,
        max_sessions: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl_seconds
        self.max_turns = max_turns
        self.max_bytes = max_bytes
        self.max_sessions = max_sessions
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()

    def _purge(self) -> None:
        now = self._clock()
        for sid in [k for k, (exp, _) in self._data.items() if exp <= now]:
            del self._data[sid]

    async def get(self, session_id: str) -> list[dict[str, Any]]:
        self._purge()
        item = self._data.get(session_id)
        return list(item[1]) if item else []

    async def append(self, session_id: str, turn: dict[str, Any]) -> None:
        self._purge()
        turns = [*(self._data[session_id][1] if session_id in self._data else []), turn]
        turns = turns[-self.max_turns :] if self.max_turns > 0 else []
        while len(turns) > 1 and _size(turns) > self.max_bytes:
            turns.pop(0)
        if turns and _size(turns) > self.max_bytes:
            turns = []  # a single oversized turn is dropped, never stored
        self._data[session_id] = (self._clock() + self.ttl, turns)
        self._data.move_to_end(session_id)
        while len(self._data) > self.max_sessions:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


def trim_turns(turns: list[dict[str, Any]], max_turns: int, max_bytes: int) -> list[dict[str, Any]]:
    """Apply the max-turns / max-bytes policy (used by the Postgres backend)."""
    turns = turns[-max_turns:] if max_turns > 0 else []
    while len(turns) > 1 and _size(turns) > max_bytes:
        turns.pop(0)
    if turns and _size(turns) > max_bytes:
        turns = []  # a single oversized turn is dropped, never stored
    return turns


def build_session_store(settings: Any) -> SessionStore:
    """Select the backend from ``settings.session_backend`` (memory | postgres | redis)."""
    backend = str(settings.session_backend).lower()
    if backend == "memory":
        return InMemorySessionStore(
            settings.session_ttl_seconds,
            settings.session_max_turns,
            settings.session_max_bytes,
            settings.session_max_sessions,
        )
    if backend == "postgres":
        from nl2sql.api.sessions_pg import PostgresSessionStore

        return PostgresSessionStore(
            settings.session_database_url,
            settings.session_ttl_seconds,
            settings.session_max_turns,
            settings.session_max_bytes,
            pool_max_size=settings.session_pool_max_size,
        )
    if backend == "redis":
        from nl2sql.api.sessions_redis import RedisSessionStore

        return RedisSessionStore(
            settings.session_redis_url,
            settings.session_ttl_seconds,
            settings.session_max_turns,
            settings.session_max_bytes,
            prefix=settings.session_redis_prefix,
        )
    raise ValueError(f"unknown session_backend {backend!r}; use memory, postgres or redis")
