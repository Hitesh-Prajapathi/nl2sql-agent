"""Audit log tests. Integration ones need Postgres plus db/audit.sql applied."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from nl2sql.audit.writer import AuditRecord, AuditWriter, hash_question
from nl2sql.config import get_settings


def _rec(rid: str) -> AuditRecord:
    return AuditRecord(
        request_id=rid,
        key_id="k1",
        question_hash=hash_question("how many?"),
        sql_text="SELECT 1",
        status="answered",
        blocked_layer=None,
        row_count=1,
        latency_ms=12.5,
        cost_usd=0.001,
        trace_id="abc",
    )


async def test_write_never_raises_when_db_down(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "audit_enabled", True)
    monkeypatch.setattr(s, "audit_database_url", "postgresql://x:y@127.0.0.1:1/none")
    w = AuditWriter()
    assert await w.write(_rec("r")) is False
    assert w.failures == 1
    assert await w.write(_rec("r")) is False  # backoff window: no new connection attempt
    assert w.failures == 1
    assert await w.spent_today_usd() is None


async def test_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "audit_enabled", False)
    assert await AuditWriter().write(_rec("r")) is False


def test_question_hash_is_stable_and_not_plaintext() -> None:
    assert hash_question(" hi ") == hash_question("hi") and "hi" not in hash_question("hi")


@pytest.mark.integration
async def test_append_only_roundtrip() -> None:
    s = get_settings()
    rid = f"test-{uuid.uuid4().hex}"
    w = AuditWriter()
    try:
        assert await w.write(_rec(rid)) is True
        assert (await w.spent_today_usd() or 0) >= 0.001
    finally:
        await w.close()

    admin = await asyncpg.connect(s.database_admin_url)
    try:
        row = await admin.fetchrow("SELECT * FROM audit.query_log WHERE request_id=$1", rid)
        assert row is not None and row["sql_text"] == "SELECT 1" and row["trace_id"] == "abc"
        with pytest.raises(asyncpg.PostgresError):
            await admin.execute("UPDATE audit.query_log SET status='x' WHERE request_id=$1", rid)
        with pytest.raises(asyncpg.PostgresError):
            await admin.execute("DELETE FROM audit.query_log WHERE request_id=$1", rid)
    finally:
        await admin.close()


@pytest.mark.integration
async def test_writer_role_is_insert_only_and_reader_has_no_access() -> None:
    s = get_settings()
    wconn = await asyncpg.connect(s.audit_database_url)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await wconn.fetch("SELECT sql_text FROM audit.query_log LIMIT 1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await wconn.execute("UPDATE audit.query_log SET status='x'")
    finally:
        await wconn.close()
    rconn = await asyncpg.connect(s.database_url)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await rconn.fetch("SELECT 1 FROM audit.query_log LIMIT 1")
    finally:
        await rconn.close()
