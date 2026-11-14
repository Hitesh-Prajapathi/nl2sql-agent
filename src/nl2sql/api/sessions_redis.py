"""Redis-backed SessionStore (``redis.asyncio``; install with the ``redis`` extra).

Each thread is a Redis list of JSON-encoded turns. The append (push, trim by turns,
trim by bytes, refresh TTL) is a single Lua script, so it is atomic server-side and
concurrent appends cannot interleave. The byte budget reproduces the in-memory
store's ``len(json.dumps(turns))`` exactly: ``2 + sum(len) + 2 * (n - 1)``.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from redis.asyncio import Redis
except ImportError as exc:  # pragma: no cover
    raise ImportError("Redis sessions need the extra: uv sync --extra redis") from exc

_APPEND_LUA = """
local key = KEYS[1]
local max_turns = tonumber(ARGV[2])
local max_bytes = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
if max_turns <= 0 then
  redis.call('DEL', key)
  return 0
end
redis.call('RPUSH', key, ARGV[1])
while redis.call('LLEN', key) > max_turns do redis.call('LPOP', key) end
local function size()
  local n = redis.call('LLEN', key)
  local total = 2 + 2 * (n - 1)
  for _, t in ipairs(redis.call('LRANGE', key, 0, -1)) do total = total + #t end
  return n, total
end
local n, total = size()
while n > 1 and total > max_bytes do
  redis.call('LPOP', key)
  n, total = size()
end
if n == 1 and total > max_bytes then
  redis.call('DEL', key)
  return 0
end
redis.call('PEXPIRE', key, ttl_ms)
return n
"""


class RedisSessionStore:
    def __init__(
        self,
        url: str,
        ttl_seconds: float,
        max_turns: int,
        max_bytes: int,
        prefix: str = "nl2sql:session:",
    ) -> None:
        self.ttl = float(ttl_seconds)
        self.max_turns = max_turns
        self.max_bytes = max_bytes
        self.prefix = prefix
        self._client: Redis = Redis.from_url(url, decode_responses=True)
        self._append = self._client.register_script(_APPEND_LUA)

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self, session_id: str) -> list[dict[str, Any]]:
        raw = await self._client.lrange(self.prefix + session_id, 0, -1)
        return [json.loads(t) for t in raw]

    async def append(self, session_id: str, turn: dict[str, Any]) -> None:
        await self._append(
            keys=[self.prefix + session_id],
            args=[
                json.dumps(turn, default=str),
                self.max_turns,
                self.max_bytes,
                max(1, int(self.ttl * 1000)),
            ],
        )
