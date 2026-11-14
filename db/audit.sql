-- ============================================================
-- Append-only audit log (Layer 7). Idempotent; run as superuser.
-- Written by the audit_writer role over a separate small pool.
-- The read-only agent role (nl2sql_reader) gets NO access here.
--
-- Retention: rows are kept 90 days online; archive/purge older rows with a
-- superuser-run job (the writer role cannot delete):
--   DELETE FROM audit.query_log WHERE ts < now() - interval '90 days';
-- (the superuser must first `ALTER TABLE audit.query_log DISABLE TRIGGER
--  query_log_append_only` for the purge window, or drop old partitions.)
-- ============================================================

CREATE SCHEMA IF NOT EXISTS audit;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'audit_writer') THEN
        CREATE ROLE audit_writer WITH LOGIN PASSWORD 'writer_pass';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS audit.query_log (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              timestamptz NOT NULL DEFAULT now(),
    request_id      text NOT NULL,
    key_id          text,
    question_hash   text NOT NULL,
    sql_text        text,
    status          text NOT NULL,
    blocked_layer   text,
    row_count       integer,
    latency_ms      double precision,
    trace_id        text
);

CREATE INDEX IF NOT EXISTS query_log_ts_idx ON audit.query_log (ts);
CREATE INDEX IF NOT EXISTS query_log_request_idx ON audit.query_log (request_id);

CREATE OR REPLACE FUNCTION audit.reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit.query_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS query_log_append_only ON audit.query_log;
CREATE TRIGGER query_log_append_only
    BEFORE UPDATE OR DELETE ON audit.query_log
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();

DROP TRIGGER IF EXISTS query_log_no_truncate ON audit.query_log;
CREATE TRIGGER query_log_no_truncate
    BEFORE TRUNCATE ON audit.query_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();

REVOKE ALL ON SCHEMA audit FROM PUBLIC;
REVOKE ALL ON audit.query_log FROM PUBLIC;
GRANT CONNECT ON DATABASE nl2sql_assignment TO audit_writer;
GRANT USAGE ON SCHEMA audit TO audit_writer;
GRANT INSERT ON audit.query_log TO audit_writer;
