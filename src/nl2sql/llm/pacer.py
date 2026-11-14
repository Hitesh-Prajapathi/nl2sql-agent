"""Client-side rate-limit pacing for the Groq free tier.

Limits (per model): ``tpm`` tokens per minute and ``rpd`` requests per day. The pacer

* keeps one token bucket per model (capacity ``tpm``, refilling ``tpm / 60`` tokens per second).
  A request reserves ``estimated prompt tokens + max output tokens`` before it is sent (Groq
  checks the requested total against the per-minute limit) and gets the unused part back when the
  call finishes (``settle``);
* counts every HTTP attempt (including ones answered with 429) against a per-model, per-UTC-day
  request budget that is persisted to a small JSON file, so a run stopped by the daily limit
  resumes correctly the next day and several processes share one count;
* on a 429 logs the full message and rate-limit headers, then reads ``retry-after`` (and the
  error text) to set a shared cooldown. A 429 whose message explicitly names a per-day limit
  ("tokens per day", "(TPD)", "requests per day", "(RPD)", "per day") is a *daily* 429: it
  backs off (``retry-after``, else 5/10/20/30 min) and the same question is retried. Only
  after ``daily_max_strikes`` consecutive daily 429s spanning ``daily_min_span_s`` is the model
  marked exhausted (with a timestamp; stale flags are cleared when a new process starts);
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

import structlog

log = structlog.get_logger(__name__)

DAILY_MARKER = "daily request budget"
DEFAULT_COOLDOWN_S = 20.0
# A non-daily 429 never makes us wait longer than this, whatever ``retry-after`` says.
MAX_COOLDOWN_S = 900.0
# Daily (TPD/RPD) 429 handling: escalating backoff, then stop the run as resumable.
DAILY_BACKOFF_S: tuple[float, ...] = (300.0, 600.0, 1200.0, 1800.0)
DAILY_MAX_STRIKES = 4
DAILY_MIN_SPAN_S = 3600.0
MAX_DAILY_WAIT_S = 3600.0
# A persisted ``exhausted`` flag older than this is cleared when a new process starts.
EXHAUSTED_TTL_S = max(DAILY_BACKOFF_S)
DAILY_PHRASES = ("tokens per day", "(tpd)", "requests per day", "(rpd)", "per day")
_SECRET = re.compile(r"(gsk_|sk-)[A-Za-z0-9_\-]{8,}")


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
    headers: dict[str, str] = field(default_factory=dict)


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
    daily = any(k in low for k in DAILY_PHRASES)
    remaining: float | None
    try:
        remaining = float(h["x-ratelimit-remaining-tokens"])
    except (KeyError, ValueError):
        remaining = None
    shown = {k: v for k, v in h.items() if k == "retry-after" or k.startswith("x-ratelimit-")}
    return RateLimitInfo(
        retry_after_s=retry,
        daily=daily,
        remaining_tokens=remaining,
        message=_SECRET.sub("[redacted]", message)[:500],
        headers=shown,
    )


@dataclass
class _ModelState:
    tokens: float
    updated: float
    cooldown_until: float = 0.0
    daily_strikes: int = 0
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
        daily_max_strikes: int = DAILY_MAX_STRIKES,
        daily_min_span_s: float = DAILY_MIN_SPAN_S,
        daily_backoff_s: tuple[float, ...] = DAILY_BACKOFF_S,
    ) -> None:
        self.daily_max_strikes = max(1, daily_max_strikes)
        self.daily_min_span_s = daily_min_span_s
        self.daily_backoff_s = daily_backoff_s or DAILY_BACKOFF_S
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
        self._expire_stale_state()

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

    def _expire_stale_state(self) -> None:
        """New process: drop ``exhausted`` / strike state older than the backoff window.

        Flags written without a timestamp (older versions) count as stale.
        """
        if self._path is None:
            return
        now = self._utcnow()

        def age(entry: dict[str, Any], key: str) -> float:
            try:
                return (now - datetime.fromisoformat(str(entry[key]))).total_seconds()
            except (KeyError, ValueError, TypeError):
                return float("inf")

        with self._flock():
            data = self._read()
            if data.get("date") != now.strftime("%Y-%m-%d"):
                return
            changed = False
            for entry in data.get("models", {}).values():
                if entry.get("exhausted") and age(entry, "exhausted_at") > EXHAUSTED_TTL_S:
                    entry["exhausted"] = False
                    entry.pop("exhausted_at", None)
                    changed = True
                if entry.get("daily_strikes") and age(entry, "last_daily_at") > EXHAUSTED_TTL_S:
                    entry["daily_strikes"] = 0
                    changed = True
            if changed:
                self._write(data)

    def reset_exhausted(self) -> list[str]:
        """Clear today's exhausted flag and daily-429 strikes; returns the models changed."""
        today = self._utcnow().strftime("%Y-%m-%d")
        cleared: list[str] = []
        with self._flock():
            data = self._read()
            if data.get("date") != today:
                return cleared
            for model, entry in data.get("models", {}).items():
                if entry.get("exhausted") or entry.get("daily_strikes"):
                    entry["exhausted"] = False
                    entry["daily_strikes"] = 0
                    entry.pop("exhausted_at", None)
                    cleared.append(model)
            if cleared:
                self._write(data)
        self._models.clear()
        return cleared

    def daily_strikes(self, model: str) -> int:
        """Consecutive daily 429s so far (0 after any success)."""
        return int(self._day(model).get("daily_strikes", 0))

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
        st = self._state(model)
        if st.daily_strikes or self._day(model).get("daily_strikes"):
            st.daily_strikes = 0
            self._day(model, lambda e: e.__setitem__("daily_strikes", 0))
        if actual is None:
            return
        st.tokens = min(float(self.tpm), st.tokens + reserved - actual)

    def note_rate_limited(self, model: str, exc: BaseException) -> RateLimitInfo:
        """Record a 429: log it, set the shared cooldown, and track consecutive daily 429s."""
        info = parse_rate_limit(exc)
        st = self._state(model)
        now = self._utcnow()
        iso = now.isoformat()
        now_mono = self._clock()
        # A 429 for a request sent before the current cooldown began is the same event.
        counted = now_mono >= st.cooldown_until
        out: dict[str, Any] = {}

        def mark(e: dict[str, Any]) -> None:
            e["rate_limited"] += 1
            e["last_429_message"] = info.message
            e["last_429_at"] = iso
            e["last_429_daily"] = info.daily
            if not (info.daily and counted):
                return
            strikes = int(e.get("daily_strikes", 0)) + 1
            if strikes == 1:
                e["first_daily_at"] = iso
            e["daily_strikes"] = strikes
            e["last_daily_at"] = iso
            try:
                first = datetime.fromisoformat(str(e["first_daily_at"]))
            except (KeyError, ValueError):
                first = now
            out["strikes"] = strikes
            out["span"] = (now - first).total_seconds()
            if strikes >= self.daily_max_strikes and out["span"] >= self.daily_min_span_s:
                e["exhausted"] = True
                e["exhausted_at"] = iso
                out["stop"] = True

        self._day(model, mark)
        log.warning(
            "llm_rate_limited",
            model=model,
            daily=info.daily,
            message=info.message,
            headers=info.headers,
            daily_strikes=out.get("strikes", 0),
            exhausted=bool(out.get("stop")),
        )
        if info.daily:
            if "strikes" in out:
                st.daily_strikes = out["strikes"]
                if not out.get("stop"):
                    sched = self.daily_backoff_s
                    wait = (
                        min(info.retry_after_s, MAX_DAILY_WAIT_S)
                        if info.retry_after_s is not None
                        else sched[min(out["strikes"], len(sched)) - 1]
                    )
                    st.cooldown_until = max(st.cooldown_until, now_mono + wait + 0.5)
        else:
            wait = info.retry_after_s if info.retry_after_s is not None else DEFAULT_COOLDOWN_S
            wait = min(wait, MAX_COOLDOWN_S)
            st.cooldown_until = max(st.cooldown_until, now_mono + wait + 0.5)
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
