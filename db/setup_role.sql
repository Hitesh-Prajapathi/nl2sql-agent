-- ============================================================
-- NL-to-SQL Agent — Read-Only Role Setup
-- Run AFTER schema.sql and seed_data.sql
-- ============================================================

-- Create a reader role with SELECT-only access
-- This is Layer 1 of the 7-layer safety model — the real guarantee.
-- Even if sqlglot validation and read-only transactions both fail,
-- the DB role physically cannot write.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'nl2sql_reader') THEN
        CREATE ROLE nl2sql_reader WITH LOGIN PASSWORD 'reader_pass';
    END IF;
END
$$;

-- Grant minimal privileges
GRANT CONNECT ON DATABASE nl2sql_assignment TO nl2sql_reader;
GRANT USAGE ON SCHEMA public TO nl2sql_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nl2sql_reader;

-- Ensure future tables are also covered
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO nl2sql_reader;

-- Enforce read-only at session level as additional defence-in-depth
ALTER ROLE nl2sql_reader SET default_transaction_read_only = on;
ALTER ROLE nl2sql_reader SET statement_timeout = '15000';
ALTER ROLE nl2sql_reader SET lock_timeout = '5000';
ALTER ROLE nl2sql_reader SET idle_in_transaction_session_timeout = '30000';
