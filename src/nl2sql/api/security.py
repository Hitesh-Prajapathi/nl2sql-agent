"""API-key auth (hashed keys) and rate limiting (token bucket behind an interface)."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from nl2sql.config import get_settings


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_key_hashes(spec: str) -> dict[str, str]:
    """'id:sha256hex,id2:sha256hex' -> {id: hex}. Malformed entries are ignored."""
    out: dict[str, str] = {}
    for part in spec.split(","):
        kid, sep, digest = part.strip().partition(":")
        if sep and kid and digest:
            out[kid] = digest.lower()
    return out


def authenticate(provided: str | None) -> str | None:
    """Return the key ID for a valid key, else None. Caller checks auth_enabled first."""
    if not provided:
        return None
    digest = hash_key(provided)
    match: str | None = None
    for kid, expected in parse_key_hashes(get_settings().api_key_hashes).items():
        if hmac.compare_digest(digest, expected):
            match = kid
    return match


def auth_enabled() -> bool:
    return bool(parse_key_hashes(get_settings().api_key_hashes))


class RateLimiter(Protocol):
    async def allow(self, key: str, per_minute: int) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds). Redis implementations drop in here."""
        ...


@dataclass
class _Bucket:
    tokens: float
    ts: float


class InMemoryTokenBucket:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}

    async def allow(self, key: str, per_minute: int) -> tuple[bool, float]:
        if per_minute <= 0:
            return True, 0.0
        now = self._clock()
        rate = per_minute / 60.0
        b = self._buckets.get(key)
        if b is None:
            if len(self._buckets) >= self._max_keys:
                self._buckets.pop(next(iter(self._buckets)))
            b = self._buckets[key] = _Bucket(float(per_minute), now)
        b.tokens = min(float(per_minute), b.tokens + (now - b.ts) * rate)
        b.ts = now
        if b.tokens >= 1:
            b.tokens -= 1
            return True, 0.0
        return False, (1 - b.tokens) / rate
