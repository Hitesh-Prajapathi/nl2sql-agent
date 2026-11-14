-- ============================================================
-- Shared API state for multi-worker deployments. Idempotent; run as superuser:
--   psql -U postgres -d nl2sql_assignment -f db/app_state.sql
-- Session history per thread_id, written by the app_state_writer role over its own
-- small pool. Touches nothing outside the app_state schema.
-- ============================================================

CREATE SCHEMA IF NOT EXISTS app_state;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'app_state_writer') THEN
        CREATE ROLE app_state_writer WITH LOGIN PASSWORD 'app_state_pass';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS app_state.sessions (
    session_id  text PRIMARY KEY,
    turns       jsonb NOT NULL DEFAULT '[]'::jsonb,
    expires_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON app_state.sessions (expires_at);

REVOKE ALL ON SCHEMA app_state FROM PUBLIC;
REVOKE ALL ON app_state.sessions FROM PUBLIC;
GRANT CONNECT ON DATABASE nl2sql_assignment TO app_state_writer;
GRANT USAGE ON SCHEMA app_state TO app_state_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_state.sessions TO app_state_writer;
-- asyncpg's pool resets each released connection with pg_advisory_unlock_all(); hardened
-- databases that revoke EXECUTE on pg_catalog functions from PUBLIC need this grant.
GRANT EXECUTE ON FUNCTION pg_catalog.pg_advisory_unlock_all() TO app_state_writer;
