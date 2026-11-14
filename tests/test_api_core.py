"""API hardening tests (offline: fake LLM + stub schema + fake executor)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nl2sql.agent.graph import build_graph
from nl2sql.agent.nodes import execute as execute_mod
from nl2sql.api.app import create_app
from nl2sql.api.security import InMemoryTokenBucket, hash_key, parse_key_hashes
from nl2sql.api.sessions import InMemorySessionStore
from nl2sql.audit.writer import AuditWriter
from nl2sql.config import get_settings
from nl2sql.llm.providers import FakeLLMScript
from tests.fakes import (
    FakeExecutor,
    StubSchemaProvider,
    err_result,
    ok_result,
    refuse_plan,
    sql_plan,
)

GOOD_SQL = "SELECT COUNT(*) AS total FROM customers"
# > 50 rows forces the LLM narration path in synthesize (usage is reported there)
BIG = FakeExecutor(ok_result([{"n": i} for i in range(60)]))


class NullAudit(AuditWriter):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[Any] = []

    async def write(self, rec: Any) -> bool:
        self.records.append(rec)
        return True


@pytest.fixture
def audit() -> NullAudit:
    return NullAudit()


async def _factory() -> Any:
    return build_graph(None, schema_provider=StubSchemaProvider())


async def _ready() -> None:
    return None


@pytest.fixture
def make_client(
    fake_llm: FakeLLMScript, monkeypatch: pytest.MonkeyPatch, audit: NullAudit
) -> Iterator[Any]:
    clients: list[TestClient] = []
    monkeypatch.setattr(execute_mod, "execute_sql", FakeExecutor(ok_result()))
    s = get_settings()
    monkeypatch.setattr(s, "audit_enabled", False)
    monkeypatch.setattr(s, "api_key_hashes", "")

    def _make(executor: FakeExecutor | None = None, **kw: Any) -> TestClient:
        if executor:
            monkeypatch.setattr(execute_mod, "execute_sql", executor)
        app = create_app(graph_factory=_factory, ready_check=_ready, audit=audit, **kw)
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def test_query_and_v1_alias(make_client: Any, fake_llm: FakeLLMScript) -> None:
    fake_llm.plan(sql_plan(GOOD_SQL))
    c = make_client()
    for path in ("/query", "/v1/query"):
        r = c.post(path, json={"question": "How many customers?"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "answered" and body["request_id"]
        assert r.headers["x-request-id"] == body["request_id"]


def test_unknown_database_fails_without_llm_call(make_client: Any, fake_llm: FakeLLMScript) -> None:
    c = make_client()
    r = c.post("/v1/query", json={"question": "q", "database": "no_such_db"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed" and "no_such_db" in (body["answer"] or "")
    assert fake_llm.count("plan") == 0


def test_known_database_field_is_accepted(make_client: Any, fake_llm: FakeLLMScript) -> None:
    fake_llm.plan(sql_plan(GOOD_SQL))
    r = make_client().post("/v1/query", json={"question": "q", "database": "default"})
    assert r.json()["status"] == "answered"


def test_request_id_echoed(make_client: Any) -> None:
    r = make_client().get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


def test_health_endpoints(make_client: Any) -> None:
    c = make_client()
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 200


def test_readyz_failure_has_no_detail(fake_llm: FakeLLMScript) -> None:
    async def bad() -> None:
        raise RuntimeError("secret dsn postgres://u:p@h")

    with TestClient(create_app(graph_factory=_factory, ready_check=bad)) as c:
        r = c.get("/readyz")
    assert r.status_code == 503 and "secret" not in r.text


def test_empty_and_too_long_question(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "max_question_chars", 10)
    c = make_client()
    assert c.post("/v1/query", json={"question": "  "}).status_code == 400
    r = c.post("/v1/query", json={"question": "x" * 11})
    assert r.status_code == 413 and r.json()["error"]["code"] == "question_too_long"
    assert c.post("/v1/query", json={}).json()["error"]["code"] == "invalid_request"


def test_auth_enforced_when_configured(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "api_key_hashes", f"alice:{hash_key('s3cret')}")
    c = make_client()
    r = c.post("/v1/query", json={"question": "q"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    bad = c.post("/v1/query", json={"question": "q"}, headers={"X-API-Key": "nope"})
    assert bad.status_code == 401
    ok = c.post("/v1/query", json={"question": "q"}, headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200
    assert c.get("/healthz").status_code == 200  # probes stay open


def test_auth_disabled_without_keys(make_client: Any) -> None:
    assert make_client().post("/v1/query", json={"question": "q"}).status_code == 200


def test_parse_key_hashes() -> None:
    assert parse_key_hashes("a:AB, bad, c:") == {"a": "ab"}


def test_rate_limit_per_key_and_ip(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_per_ip_per_min", 1000)
    monkeypatch.setattr(s, "rate_limit_per_key_per_min", 2)
    monkeypatch.setattr(s, "api_key_hashes", f"k:{hash_key('x')}")
    c = make_client()
    h = {"X-API-Key": "x"}
    codes = [c.post("/v1/query", json={"question": "q"}, headers=h).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = c.post("/v1/query", json={"question": "q"}, headers=h)
    assert "retry-after" in r.headers

    monkeypatch.setattr(s, "api_key_hashes", "")
    monkeypatch.setattr(s, "rate_limit_per_ip_per_min", 1)
    c2 = make_client()
    assert c2.post("/query", json={"question": "q"}).status_code == 200
    assert c2.post("/query", json={"question": "q"}).status_code == 429


async def test_token_bucket_refill() -> None:
    now = [0.0]
    tb = InMemoryTokenBucket(clock=lambda: now[0])
    for _ in range(60):
        assert (await tb.allow("a", 60))[0]
    ok, wait = await tb.allow("a", 60)
    assert not ok and wait > 0
    now[0] += 2
    assert (await tb.allow("a", 60))[0]


def test_error_envelope_hides_exception(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    c = make_client()

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("super secret internals")

    monkeypatch.setattr("nl2sql.api.app.run_query", boom)
    r = c.post("/v1/query", json={"question": "q"})
    assert r.status_code == 500
    assert "secret" not in r.text and r.json()["error"]["code"] == "internal_error"


def test_cors_allowlist(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "cors_allow_origins", "https://ok.example")
    c = make_client()
    req = "Access-Control-Request-Method"
    good = c.options("/v1/query", headers={"Origin": "https://ok.example", req: "POST"})
    bad = c.options("/v1/query", headers={"Origin": "https://evil.example", req: "POST"})
    assert good.headers.get("access-control-allow-origin") == "https://ok.example"
    assert "access-control-allow-origin" not in bad.headers


def test_token_usage_reported_without_cost(make_client: Any, fake_llm: FakeLLMScript) -> None:
    fake_llm.usage = (5, 5)
    fake_llm.plan(sql_plan(GOOD_SQL))
    body = make_client(BIG).post("/v1/query", json={"question": "q"}).json()
    assert body["input_tokens"] == 5 and body["output_tokens"] == 5
    assert "cost_usd" not in body


def test_kill_switch(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "llm_kill_switch", True)
    r = make_client().post("/v1/query", json={"question": "q"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "kill_switch"


def test_audit_records_and_blocking_layer(
    make_client: Any, fake_llm: FakeLLMScript, audit: NullAudit
) -> None:
    fake_llm.plan(sql_plan("DROP TABLE customers"))
    r = make_client().post("/v1/query", json={"question": "drop it"})
    assert r.json()["status"] == "refused"
    rec = audit.records[-1]
    assert rec.blocked_layer == "ast_validator" and rec.status == "refused"
    assert rec.question_hash != "drop it" and len(rec.question_hash) == 64
    assert rec.request_id == r.json()["request_id"] and rec.trace_id


def test_audit_db_layer(make_client: Any, fake_llm: FakeLLMScript, audit: NullAudit) -> None:
    fake_llm.plan(sql_plan(GOOD_SQL))
    c = make_client(FakeExecutor(err_result("permission denied", safety=True)))
    c.post("/v1/query", json={"question": "q"})
    assert audit.records[-1].blocked_layer == "database"


def test_audit_failure_never_fails_request(
    fake_llm: FakeLLMScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_llm.plan(sql_plan(GOOD_SQL))
    monkeypatch.setattr(execute_mod, "execute_sql", FakeExecutor(ok_result()))
    s = get_settings()
    monkeypatch.setattr(s, "audit_enabled", True)
    monkeypatch.setattr(s, "audit_database_url", "postgresql://x:y@127.0.0.1:1/none")
    monkeypatch.setattr(s, "api_key_hashes", "")
    with TestClient(create_app(graph_factory=_factory)) as c:
        assert c.post("/v1/query", json={"question": "q"}).status_code == 200


def test_refusal_is_not_a_block(
    make_client: Any, fake_llm: FakeLLMScript, audit: NullAudit
) -> None:
    fake_llm.plan(refuse_plan("no"))
    make_client().post("/v1/query", json={"question": "q"})
    assert audit.records[-1].blocked_layer is None


def test_session_history_accumulates(make_client: Any, fake_llm: FakeLLMScript) -> None:
    fake_llm.plan(sql_plan(GOOD_SQL))
    c = make_client()
    c.post("/v1/query", json={"question": "first", "thread_id": "t1"})
    c.post("/v1/query", json={"question": "second", "thread_id": "t1"})
    hist = c.app.state.sessions._data["t1"][1]
    assert [t["question"] for t in hist] == ["first", "second"]


async def test_session_store_limits() -> None:
    now = [0.0]
    st = InMemorySessionStore(10, max_turns=2, max_bytes=400, max_sessions=2, clock=lambda: now[0])
    for i in range(3):
        await st.append("a", {"question": f"q{i}"})
    assert [t["question"] for t in await st.get("a")] == ["q1", "q2"]
    await st.append("a", {"question": "x" * 1000})  # oversized turn is dropped
    assert sum(len(json.dumps(t)) for t in await st.get("a")) <= 400
    await st.append("b", {"question": "b"})
    await st.append("c", {"question": "c"})  # evicts the LRU session
    assert len(st) == 2 and await st.get("a") == []
    now[0] += 11
    assert await st.get("c") == []  # TTL


def test_metrics_endpoint(make_client: Any, fake_llm: FakeLLMScript) -> None:
    fake_llm.plan(sql_plan(GOOD_SQL))
    c = make_client()
    c.post("/v1/query", json={"question": "q"})
    text = c.get("/metrics").text
    for name in (
        "nl2sql_request_latency_seconds",
        "nl2sql_requests_total",
        "nl2sql_blocks_total",
        "nl2sql_llm_retries_total",
        "nl2sql_llm_tokens_total",
        "nl2sql_node_latency_seconds",
    ):
        assert name in text


def test_node_spans_and_request_span(make_client: Any, fake_llm: FakeLLMScript) -> None:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from nl2sql.telemetry.tracing import setup_tracing

    exp = InMemorySpanExporter()
    setup_tracing().add_span_processor(SimpleSpanProcessor(exp))
    fake_llm.plan(sql_plan(GOOD_SQL))
    make_client().post("/v1/query", json={"question": "q"})
    spans = exp.get_finished_spans()
    names = {sp.name for sp in spans}
    assert "request" in names and {"node.plan_sql", "node.execute_sql"} <= names
    req = next(sp for sp in spans if sp.name == "request")
    node = next(sp for sp in spans if sp.name == "node.plan_sql")
    assert node.context is not None and req.context is not None
    assert node.context.trace_id == req.context.trace_id


def test_stream_sse(make_client: Any, fake_llm: FakeLLMScript) -> None:
    fake_llm.plan(sql_plan(GOOD_SQL))
    c = make_client()
    with c.stream("POST", "/v1/query/stream", json={"question": "q"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = "".join(r.iter_text()).splitlines()
    events = [ln[7:] for ln in lines if ln.startswith("event: ")]
    assert events[-1] == "result"
    nodes = [
        json.loads(b[6:])["node"]
        for a, b in zip(lines, lines[1:], strict=False)
        if a == "event: node"
    ]
    assert nodes[:2] == ["load_schema", "plan_sql"] and "synthesize" in nodes
    final = json.loads([ln for ln in lines if ln.startswith("data: ")][-1][6:])
    assert final["status"] == "answered"


def test_stream_validation_error_is_json(make_client: Any) -> None:
    r = make_client().post("/v1/query/stream", json={"question": ""})
    assert r.status_code == 400 and r.json()["error"]["code"] == "empty_question"
