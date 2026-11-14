"""LLM kill switch.

Formerly also a daily USD spend cap. Cost tracking was removed (the Groq free tier has no prices),
so only the kill switch remains. The class keeps its name for the existing wiring.
"""

from __future__ import annotations

from nl2sql.config import get_settings


class SpendGuard:
    def __init__(self) -> None:
        self.killed = False  # runtime kill switch (also settings.llm_kill_switch)

    async def reason_blocked(self) -> str | None:
        """None if the request may proceed, else 'kill_switch'."""
        if self.killed or get_settings().llm_kill_switch:
            return "kill_switch"
        return None
