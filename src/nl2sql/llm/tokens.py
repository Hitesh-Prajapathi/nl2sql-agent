"""Conservative, dependency-free token estimates for rate-limit pacing and prompt caps.

The estimate deliberately over-counts a little: ASCII text at 3.5 characters per token and every
non-ASCII character (the box-drawing rules in the planner prompt, accented values) as a whole
token. The eval checks calibrate it against the usage the provider actually reports.
"""

from __future__ import annotations

import math
from typing import Any

ASCII_CHARS_PER_TOKEN = 3.5
# Role markers / separators the chat template adds around every message.
MESSAGE_OVERHEAD_TOKENS = 8


def estimate_text_tokens(text: str) -> int:
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    other = len(text) - ascii_chars
    return math.ceil(ascii_chars / ASCII_CHARS_PER_TOKEN) + other


def estimate_messages_tokens(messages: Any) -> int:
    """Estimated prompt tokens for a LangChain-style message list (dicts, tuples or messages)."""
    if isinstance(messages, str):
        return estimate_text_tokens(messages) + MESSAGE_OVERHEAD_TOKENS
    total = 0
    for m in messages:
        if isinstance(m, dict):
            content = m.get("content", "")
        elif isinstance(m, tuple) and len(m) == 2:
            content = m[1]
        else:
            content = getattr(m, "content", m)
        total += estimate_text_tokens(str(content)) + MESSAGE_OVERHEAD_TOKENS
    return total
