"""Daily spend cap and kill switch."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from nl2sql.config import get_settings

_SYNC_INTERVAL_S = 30.0


class SpendGuard:
    """Tracks today's spend (UTC day). ``sync`` optionally reads a shared total
    (the audit log) so several workers converge on one number."""

    def __init__(
        self,
        sync: Callable[[], Awaitable[float | None]] | None = None,
        today: Callable[[], str] | None = None,
    ) -> None:
        self._sync = sync
        self._today = today or (lambda: datetime.now(UTC).strftime("%Y-%m-%d"))
        self._day = self._today()
        self.spent = 0.0
        self.killed = False  # runtime kill switch (also settings.llm_kill_switch)
        self._last_sync = 0.0

    def _roll(self) -> None:
        d = self._today()
        if d != self._day:
            self._day, self.spent = d, 0.0

    def record(self, cost: float | None) -> None:
        self._roll()
        if cost:
            self.spent += cost

    async def reason_blocked(self) -> str | None:
        """None if the request may proceed, else 'kill_switch' or 'daily_cap'."""
        s = get_settings()
        if self.killed or s.llm_kill_switch:
            return "kill_switch"
        if s.daily_spend_cap_usd is None:
            return None
        self._roll()
        if self._sync is not None and time.monotonic() - self._last_sync > _SYNC_INTERVAL_S:
            self._last_sync = time.monotonic()
            shared = await self._sync()
            if shared is not None:
                self.spent = max(self.spent, shared)
        return "daily_cap" if self.spent >= s.daily_spend_cap_usd else None
