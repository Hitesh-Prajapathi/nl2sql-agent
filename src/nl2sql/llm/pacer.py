"""Client-side rate-limit pacing for the Groq free tier.

Limits (per model): ``tpm`` tokens per minute and ``rpd`` requests per day. The pacer

* keeps one token bucket per model (capacity ``tpm``, refilling ``tpm / 60`` tokens per second).
  A request reserves ``estimated prompt tokens + max output tokens`` before it is sent (Groq
  checks the requested total against the per-minute limit) and gets the unused part back when the
  call finishes (``settle``);
* counts every HTTP attempt (including ones answered with 429) against a per-model, per-UTC-day
  request budget that is persisted to a small JSON file, so a run stopped by the daily limit
  resumes correctly the next day and several processes share one count;
* on a 429 reads ``retry-after`` / ``x-ratelimit-*`` (and the error text) to set a shared
  cooldown, or marks the model as exhausted for the day when the daily limit was hit;
* refuses locally (``RequestTooLarge``, text contains "413") a request bigger than the bucket,
  instead of waiting for tokens that can never accumulate.

``DailyBudgetExhausted`` is never retried and never falls back to another model: the caller is
expected to stop cleanly and resume later.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DAILY_MARKER = "daily request budget"
DEFAULT_COOLDOWN_S = 20.0
# A 429 asking us to wait longer than this is a daily limit, not a per-minute one.
DAILY_RETRY_AFTER_S = 900.0


class DailyBudgetExhausted(Exception):
    """The model's daily request budget is used up (or Groq said so). Stop and resume later."""

    def __init__(self, model: str, used: int = 0, limit: int = 0) -> None:
        self.model = model
        super().__init__(
            f"{DAILY_MARKER} exhausted for {model} ({used}/{limit} requests today, UTC day)"
        )


class RequestTooLarge(Exception):
    """HTTP 413 equivalent raised locally: the request alone exceeds the per-minute token limit."""

    status_code = 413


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(text: str | None) -> float | None:
    """'7.66s', '2m59.56s', '1h2m', '120ms' or plain seconds ('30') -> seconds."""
    if not text:
        return None
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    parts = _DURATION.findall(text)
    if not parts:
        return None
    return sum(float(v) * _UNIT_S[u] for v, u in parts)


@dataclass
class RateLimitInfo:
    retry_after_s: float | None = None
    daily: bool = False
    remaining_tokens: float | None = None
    message: str = ""


def _headers_of(exc: BaseException) -> dict[str, str]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return {}
    try:
        return {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except (TypeError, ValueError):
        return {}


def parse_rate_limit(exc: BaseException) -> RateLimitInfo:
    """Extract retry hints from a 429 (SDK exception with ``.response.headers`` and a message)."""
    h = _headers_of(exc)
    message = str(exc)
    low = message.lower()
    retry = parse_duration(h.get("retry-after"))
    if retry is None:
        retry = parse_duration(h.get("x-ratelimit-reset-tokens"))
    reset_req = parse_duration(h.get("x-ratelimit-reset-requests"))
    daily = any(k in low for k in ("per day", "(rpd)", "(tpd)", "requests per day", "daily"))
    if retry is not None and retry > DAILY_RETRY_AFTER_S:
        daily = True
    if reset_req is not None and reset_req > DAILY_RETRY_AFTER_S and "request" in low:
        daily = True
    remaining: float | None
    try:
        remaining = float(h["x-ratelimit-remaining-tokens"])
    except (KeyError, ValueError):
        remaining = None
    return RateLimitInfo(
        retry_after_s=retry, daily=daily, remaining_tokens=remaining, message=message[:300]
    )


@dataclass
class _ModelState:
    tokens: float
    updated: float
    cooldown_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Pacer:
    def __init__(
        self,
        tpm: int = 8000,
        rpd: int = 1000,
        *,
        state_path: str | Path | None = None,
        rpd_margin: int = 5,
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.tpm = tpm
        self.rpd = rpd
        self.rpd_margin = rpd_margin
        self._path = Path(state_path) if state_path else None
        self._clock = clock
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._rate = tpm / 60.0
        self._models: dict[str, _ModelState] = {}
        self._mem: dict[str, Any] = {}

    # ── per-minute token bucket ──────────────────────────────────────────────

    def _state(self, model: str) -> _ModelState:
        st = self._models.get(model)
        if st is None:
            st = self._models[model] = _ModelState(tokens=float(self.tpm), updated=self._clock())
        return st

    def _refill(self, st: _ModelState, now: float) -> None:
        st.tokens = min(float(self.tpm), st.tokens + (now - st.updated) * self._rate)
        st.updated = now

    # ── per-day request budget (persisted, shared across processes) ──────────

    def _day(
        self, model: str, mutate: Callable[[dict[str, Any]], None] | None = None
    ) -> dict[str, Any]:
        """The model's counters for today's UTC day, optionally mutated and persisted atomically."""
        today = self._utcnow().strftime("%Y-%m-%d")
        with self._flock():
            data = self._read()
            if data.get("date") != today:
                data = {"date": today, "models": {}}
            entry = data.setdefault("models", {}).setdefault(
                model, {"requests": 0, "rate_limited": 0, "exhausted": False}
            )
            if mutate is not None:
                mutate(entry)
                self._write(data)
            return dict(entry)

    @contextlib.contextmanager
    def _flock(self) -> Iterator[None]:
        if self._path is None:
            yield
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if self._path is None:
            return self._mem
        try:
            loaded = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        if self._path is None:
            self._mem = data
            return
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self._path)

    def requests_today(self, model: str) -> int:
        return int(self._day(model)["requests"])

    def remaining_requests(self, model: str) -> int:
        """Requests the pacer will still admit today (0 once exhausted)."""
        day = self._day(model)
        if day["exhausted"]:
            return 0
        return max(0, self.rpd - self.rpd_margin - int(day["requests"]))

    def seconds_until_reset(self) -> float:
        now = self._utcnow()
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return (nxt - now).total_seconds()

    def snapshot(self) -> dict[str, Any]:
        with self._flock():
            data = self._read()
        return data if data.get("date") == self._utcnow().strftime("%Y-%m-%d") else {}

    # ── the API used by the LLM layer ────────────────────────────────────────

    async def acquire(self, model: str, tokens: int) -> None:
        """Wait until ``tokens`` (prompt estimate + max output) may be sent to ``model``."""
        if tokens > self.tpm:
            raise RequestTooLarge(
                f"413 request too large: ~{tokens} tokens (prompt + max output) exceeds the "
                f"{self.tpm} tokens/minute limit of {model}"
            )
        st = self._state(model)
        async with st.lock:  # FIFO per model
            while True:
                day = self._day(model)
                if day["exhausted"] or day["requests"] >= self.rpd - self.rpd_margin:
                    raise DailyBudgetExhausted(model, int(day["requests"]), self.rpd)
                now = self._clock()
                self._refill(st, now)
                wait = max(0.0, st.cooldown_until - now)
                if wait == 0.0:
                    if st.tokens >= tokens:
                        st.tokens -= tokens
                        self._day(model, lambda e: e.__setitem__("requests", e["requests"] + 1))
                        return
                    wait = (tokens - st.tokens) / self._rate
                await self._sleep(wait + 0.05)

    def settle(self, model: str, reserved: int, actual: int | None) -> None:
        """Return the unused part of a reservation once the real token usage is known."""
        if actual is None:
            return
        st = self._state(model)
        st.tokens = min(float(self.tpm), st.tokens + reserved - actual)

    def note_rate_limited(self, model: str, exc: BaseException) -> RateLimitInfo:
        """Record a 429: set the shared cooldown, or mark the model exhausted for the day."""
        info = parse_rate_limit(exc)
        st = self._state(model)

        def mark(e: dict[str, Any]) -> None:
            e["rate_limited"] += 1
            if info.daily:
                e["exhausted"] = True

        self._day(model, mark)
        if not info.daily:
            wait = info.retry_after_s if info.retry_after_s is not None else DEFAULT_COOLDOWN_S
            st.cooldown_until = max(st.cooldown_until, self._clock() + wait + 0.5)
            st.tokens = min(st.tokens, info.remaining_tokens or 0.0)
        return info


# ── process-wide instance ────────────────────────────────────────────────────

_pacer: Pacer | None = None
_pacer_installed = False


def set_pacer(pacer: Pacer | None) -> None:
    """Install (or, with None, disable) the process-wide pacer."""
    global _pacer, _pacer_installed
    _pacer, _pacer_installed = pacer, True


def reset_pacer() -> None:
    global _pacer, _pacer_installed
    _pacer, _pacer_installed = None, False


def active_pacer(provider: str, enabled: bool, tpm: int, rpd: int) -> Pacer | None:
    """The installed pacer, else (Groq only, when enabled) a lazily created in-memory one."""
    global _pacer
    if _pacer_installed:
        return _pacer
    if provider == "groq" and enabled:
        _pacer = Pacer(tpm=tpm, rpd=rpd)
        return _pacer
    return None
