-- ============================================================
-- Database-layer hardening (Phase 4.5). Idempotent; run as a SUPERUSER
-- against nl2sql_assignment, AFTER schema.sql, setup_role.sql and audit.sql.
--
--   psql -v ON_ERROR_STOP=1 -d nl2sql_assignment -f db/hardening.sql
--
-- Goal: make Postgres itself refuse the attacks that previously only the SQL
-- validator stopped. Everything is scoped to THIS database (pg_proc, pg_class
-- ACLs and language ACLs are per-database) and to PUBLIC / nl2sql_reader, so
-- other databases in the same cluster are untouched. Superusers bypass ACLs, so
-- the admin connection (postgres) is unaffected. audit_writer only does an
-- INSERT with now()/gen_random_uuid() defaults and a SELECT on two columns; it
-- needs none of the revoked objects, so no grant-back to it is required.
--
-- Not changed: table data, other roles' table grants, other databases.
-- ============================================================

-- ---------------------------------------------------------------- 1. functions
-- Revoke EXECUTE from PUBLIC on every overload of the dangerous built-ins.
-- Matching by name pattern (not a fixed signature list) so new overloads and
-- minor-version additions are covered, and so re-running is harmless.
DO $hardening$
DECLARE
    fn record;
    dangerous constant text := '^(' || array_to_string(ARRAY[
        -- string-SQL executors (run an arbitrary query given as text)
        '(query|cursor|table|schema|database)_to_xml.*', 'ts_stat',
        -- sleep / locks / resource exhaustion
        'pg_sleep.*', 'pg_(try_)?advisory_.*',
        -- config read / write
        'set_config', 'current_setting', 'pg_show_all_settings',
        'pg_show_all_file_settings', 'pg_settings_get_flags', 'pg_get_keywords',
        -- sequences and transaction / snapshot / WAL state
        'nextval', 'setval', 'currval', 'lastval',
        'txid_.*', 'pg_current_.*', 'pg_snapshot_.*', 'pg_export_snapshot',
        'pg_import_snapshot', 'pg_xact_.*', 'pg_last_.*', 'pg_wal_.*',
        'pg_switch_wal', 'pg_is_in_recovery', 'pg_is_wal_replay_paused',
        'pg_get_wal_.*', 'pg_get_replication_slots', 'pg_get_multixact_members',
        'pg_get_catalog_foreign_keys',
        -- backend control / messaging / logging
        'pg_terminate_backend', 'pg_cancel_backend', 'pg_notify',
        'pg_notification_queue_usage', 'pg_logical_emit_message',
        'pg_reload_conf', 'pg_rotate_logfile', 'pg_log_backend_memory_contexts',
        'pg_stat_reset.*', 'pg_stat_get_.*', 'pg_backend_pid', 'pg_blocking_pids',
        'pg_safe_snapshot_blocking_pids', 'pg_isolation_test_session_is_blocked',
        'pg_promote', 'pg_create_.*', 'pg_drop_.*', 'pg_replication_.*',
        'pg_backup_.*', 'pg_start_backup', 'pg_stop_backup',
        -- file access
        'pg_ls_.*', 'pg_read_.*', 'pg_stat_file', 'pg_file_.*',
        'pg_relation_filepath', 'pg_filenode_relation', 'pg_relation_filenode',
        -- large objects
        'lo_.*', 'loread', 'lowrite', 'pg_largeobject_.*',
        -- server / session / environment disclosure
        'version', 'inet_(server|client)_(addr|port)', 'pg_postmaster_start_time',
        'pg_conf_load_time', 'pg_trigger_depth',
        'pg_database_size', 'pg_tablespace_size', 'pg_tablespace_location',
        'pg_tablespace_databases', 'pg_total_relation_size', 'pg_table_size',
        'pg_indexes_size', 'pg_relation_size', 'pg_get_serial_sequence',
        'pg_get_userbyid', 'pg_get_viewdef', 'pg_get_functiondef',
        'pg_get_function_.*', 'pg_get_ruledef', 'pg_get_triggerdef',
        'pg_get_object_address', 'pg_describe_object', 'pg_identify_object.*',
        'pg_listening_channels', 'pg_timezone_.*', 'pg_options_to_table',
        'pg_collation_actual_version', 'pg_control_.*', 'pg_current_logfile',
        'pg_config', 'pg_hba_file_rules', 'pg_ident_file_mappings',
        'pg_get_shmem_allocations', 'pg_get_backend_memory_contexts',
        'has_(database|server|tablespace|language|foreign_data_wrapper|sequence|parameter|function|type)_privilege',
        'row_security_active',
        'obj_description', 'col_description', 'shobj_description',
        'current_database', 'current_schema', 'current_schemas',
        'pg_stat_statements.*', 'pg_prepared_statement', 'pg_prepared_xact',
        'pg_cursor', 'pg_lock_status', 'pg_get_publication_.*'
    ], '|') || ')$';
BEGIN
    FOR fn IN
        SELECT p.oid::regprocedure AS sig
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname IN ('pg_catalog', 'information_schema')
          AND p.prokind IN ('f', 'p')
          AND p.proname ~ dangerous
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', fn.sig);
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO postgres', fn.sig);
    END LOOP;
END
$hardening$;

-- Grant back, narrowly. The schema profiler calls current_database() as
-- nl2sql_reader (introspect.py, provider.py) to name the database in the schema
-- context. It leaks only the DB name, which is already in the DSN.
GRANT EXECUTE ON FUNCTION pg_catalog.current_database() TO nl2sql_reader;

-- information_schema.tables/columns call has_*_privilege, pg_has_role and
-- pg_my_temp_schema inside the view, and Postgres checks function EXECUTE as the
-- INVOKING role, so these must stay callable. Re-granted explicitly so a
-- re-run repairs a DB hardened by an earlier version of this script.
DO $hardening$
DECLARE
    fn record;
BEGIN
    FOR fn IN
        SELECT p.oid::regprocedure AS sig
        FROM pg_catalog.pg_proc p
        WHERE p.pronamespace = 'pg_catalog'::regnamespace
          AND p.proname IN ('pg_has_role', 'has_table_privilege', 'has_column_privilege',
                            'has_any_column_privilege', 'has_schema_privilege',
                            'pg_my_temp_schema')
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO PUBLIC', fn.sig);
    END LOOP;
END
$hardening$;

-- asyncpg's pool runs `SELECT pg_advisory_unlock_all()` (plus CLOSE ALL /
-- UNLISTEN * / RESET ALL) every time a connection is released, so the pooled
-- roles must keep this one function. It only drops the caller's OWN advisory
-- locks; acquiring locks (pg_advisory_lock*, pg_try_advisory_*) stays revoked.
-- (The other reset statements are not functions: CLOSE ALL / UNLISTEN * / RESET ALL.)
-- Also granted to every other pooled app role that exists (e.g. app_state_writer).
DO $hardening$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['nl2sql_reader', 'audit_writer', 'app_state_writer'] LOOP
        IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = r) THEN
            EXECUTE format(
                'GRANT EXECUTE ON FUNCTION pg_catalog.pg_advisory_unlock_all() TO %I', r);
        END IF;
    END LOOP;
END
$hardening$;

-- ---------------------------------------------------------------- 2. catalogs
-- Hide server-wide / operational catalog views and tables from PUBLIC. The
-- core relation catalogs (pg_class, pg_namespace, pg_attribute, pg_type, ...)
-- are NOT revoked: asyncpg's type introspection and psql-style tooling read
-- them as the connecting role. See "limits" below.
DO $hardening$
DECLARE
    rel record;
BEGIN
    FOR rel IN
        SELECT c.oid::regclass AS name
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'pg_catalog'
          AND c.relkind IN ('r', 'v', 'm')
          AND (c.relname ~ '^pg_stat' -- pg_stat_*, pg_statio_*, pg_stats*
               OR c.relname IN (
                'pg_roles', 'pg_user', 'pg_group', 'pg_shadow', 'pg_user_mappings',
                'pg_auth_members', 'pg_db_role_setting', 'pg_settings',
                'pg_file_settings', 'pg_hba_file_rules', 'pg_ident_file_mappings',
                'pg_config', 'pg_database', 'pg_tablespace', 'pg_extension',
                'pg_available_extensions', 'pg_available_extension_versions',
                'pg_locks', 'pg_prepared_xacts', 'pg_prepared_statements',
                'pg_cursors', 'pg_shmem_allocations', 'pg_backend_memory_contexts',
                'pg_replication_slots', 'pg_replication_origin',
                'pg_subscription', 'pg_publication', 'pg_publication_tables',
                'pg_foreign_server', 'pg_foreign_data_wrapper', 'pg_foreign_table',
                'pg_tables', 'pg_views', 'pg_matviews', 'pg_indexes', 'pg_index',
                'pg_policies', 'pg_rules', 'pg_proc', 'pg_trigger',
                'pg_event_trigger', 'pg_language', 'pg_amop', 'pg_amproc',
                'pg_timezone_names', 'pg_timezone_abbrevs', 'pg_collation',
                'pg_seclabel', 'pg_shseclabel', 'pg_description', 'pg_shdescription',
                'pg_inherits', 'pg_depend', 'pg_shdepend', 'pg_init_privs'))
    LOOP
        EXECUTE format('REVOKE ALL ON %s FROM PUBLIC', rel.name);
    END LOOP;
END
$hardening$;

-- information_schema: keep only the views the schema profiler reads
-- (introspect.py: tables, columns, table_constraints, key_column_usage,
-- constraint_column_usage, check_constraints) and hide the rest (routines,
-- table_privileges, role_*, schemata, parameters, ...).
DO $hardening$
DECLARE
    rel record;
BEGIN
    FOR rel IN
        SELECT c.oid::regclass AS name, c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'information_schema' AND c.relkind IN ('r', 'v')
    LOOP
        EXECUTE format('REVOKE ALL ON %s FROM PUBLIC', rel.name);
        IF rel.relname IN ('tables', 'columns', 'table_constraints',
                           'key_column_usage', 'constraint_column_usage',
                           'check_constraints', 'referential_constraints') THEN
            EXECUTE format('GRANT SELECT ON %s TO PUBLIC', rel.name);
        END IF;
    END LOOP;
END
$hardening$;

-- ---------------------------------------------------------------- 3. language / temp
-- DO blocks need USAGE on the language; plpgsql is granted to PUBLIC by default.
-- Revoking it makes `DO LANGUAGE plpgsql $$ ... $$` fail with permission denied.
-- (Existing plpgsql functions, e.g. audit.reject_mutation, keep working: USAGE is
-- checked at CREATE FUNCTION / DO time, not when a trigger fires.)
REVOKE USAGE ON LANGUAGE plpgsql FROM PUBLIC;
-- No temp tables for the read-only agent role (also stops temp-schema abuse).
REVOKE TEMPORARY ON DATABASE nl2sql_assignment FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- ---------------------------------------------------------------- 4. search_path
-- Pin the role's search_path so unqualified names resolve to public, then
-- pg_catalog, and nothing else can shadow them.
ALTER ROLE nl2sql_reader IN DATABASE nl2sql_assignment SET search_path = public, pg_catalog;

-- ---------------------------------------------------------------- limits
-- What Postgres fundamentally cannot block at the role level (validator-only):
--  * Multi-statement strings ("SELECT 1; SELECT 2"): a wire-protocol property
--    (simple query protocol). asyncpg's extended protocol rejects them, but that
--    is a client behaviour, not a server privilege.
--  * Session / transaction utility statements: SET, RESET, SET ROLE, SET
--    TRANSACTION, SHOW, BEGIN/COMMIT/ABORT/SAVEPOINT, DECLARE, PREPARE,
--    DEALLOCATE, LISTEN/UNLISTEN, NOTIFY, ANALYZE, EXPLAIN [ANALYZE] and plain
--    VACUUM have no GRANTable privilege. Every role may SET any USERSET GUC
--    (statement_timeout, default_transaction_read_only, search_path), so these
--    "knock out" the role-level session defaults; only read-only DB privileges
--    (no write grants) still hold. (GRANT SET ON PARAMETER only covers
--    SUSET parameters, not USERSET ones.)
--  * SQL-standard keyword "functions" with no pg_proc entry or ACL:
--    current_user, session_user, user, current_role, current_catalog. They
--    disclose only the role/db name the client already knows.
--  * Reading the core relation catalogs (pg_class, pg_namespace, pg_attribute,
--    pg_type, pg_constraint, information_schema.tables/columns): required by
--    asyncpg type introspection and by the schema profiler. Probing them
--    reveals only objects the role can already see; table data stays behind
--    table grants.
--  * pg_sequences, pg_largeobject_metadata stay readable: the red-team write
--    canary reads them as nl2sql_reader. They are privilege-filtered views.
--  * regclass/regproc casts ('pg_roles'::regclass) and to_regclass() look up
--    names internally; they confirm an object exists but give no data.
--  * Resource abuse by pure SQL (generate_series, cross joins, recursive CTEs):
--    bounded only by statement_timeout, which the role cannot make immutable.
-- Catalog ACL changes persist across restarts, but a major-version upgrade
-- (pg_upgrade) or initdb resets catalog ACLs: re-run this script afterwards.
