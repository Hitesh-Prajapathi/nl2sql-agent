"""Locust scenario: 70% simple, 20% complex joins, 10% multi-turn follow-ups.

Raw per-request records go to $LT_RAW (JSONL) so the runner can compute steady-state
percentiles. Think time: $LT_THINK_MIN..$LT_THINK_MAX seconds (default 0.2..1.0).
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from typing import Any

from locust import HttpUser, between, events, task

from loadtest.fake_script import COMPLEX, FOLLOWUPS, SIMPLE

_RAW = os.environ.get("LT_RAW", "")
_fh: Any = None
_SIMPLE = list(SIMPLE)
_COMPLEX = list(COMPLEX)
_OK_STATUSES = {"answered", "success", "needs_clarification", "clarification"}


@events.test_start.add_listener  # type: ignore[untyped-decorator]
def _start(environment: Any, **kw: Any) -> None:
    global _fh
    if _RAW:
        _fh = open(_RAW, "w", buffering=1)
        _fh.write(json.dumps({"meta": {"test_start": time.time()}}) + "\n")


@events.test_stop.add_listener  # type: ignore[untyped-decorator]
def _stop(environment: Any, **kw: Any) -> None:
    if _fh:
        _fh.close()


@events.request.add_listener  # type: ignore[untyped-decorator]
def _record(
    request_type: str,
    name: str,
    response_time: float,
    exception: Any,
    start_time: float | None = None,
    **kw: Any,
) -> None:
    if _fh:
        _fh.write(
            json.dumps(
                {
                    "t": start_time or time.time(),
                    "name": name,
                    "ms": response_time,
                    "ok": exception is None,
                }
            )
            + "\n"
        )


class QueryUser(HttpUser):
    wait_time = between(
        float(os.environ.get("LT_THINK_MIN", "0.2")),
        float(os.environ.get("LT_THINK_MAX", "1.0")),
    )

    def _ask(self, question: str, thread_id: str, name: str) -> None:
        with self.client.post(
            "/v1/query",
            json={"question": question, "thread_id": thread_id},
            name=name,
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            status = resp.json().get("status")
            if status not in _OK_STATUSES:
                resp.failure(f"status={status}")

    @task(70)
    def simple(self) -> None:
        self._ask(random.choice(_SIMPLE), str(uuid.uuid4()), "simple")

    @task(20)
    def complex_join(self) -> None:
        self._ask(random.choice(_COMPLEX), str(uuid.uuid4()), "complex")

    @task(10)
    def followup(self) -> None:
        first, second = random.choice(FOLLOWUPS)
        tid = str(uuid.uuid4())
        self._ask(first, tid, "followup_turn1")
        self._ask(second, tid, "followup_turn2")
