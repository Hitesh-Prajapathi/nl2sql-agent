# BIRD Mini-Dev (PostgreSQL) setup

`make bird-setup` runs `scripts/bird_setup.py`. It is idempotent.

## What ships

The official archive (`minidev.zip`, ~800 MB, URL and SHA-256 pinned in
`data/bird_minidev/MANIFEST.json`) contains `MINIDEV_postgresql/BIRD_dev.sql`: ONE pg_dump of a
single database in which all 75 tables of the 11 BIRD dev databases sit together in `public`
(there are no table-name collisions between db_ids, which the script asserts against
`dev_tables.json`). Questions are in `mini_dev_postgresql.json` (500, PostgreSQL-dialect gold SQL).

## Layout chosen: schema per db_id

1. The dump is loaded as shipped into a NEW database `bird_minidev` (OWNER lines are stripped,
   because the dump's owner role does not exist).
2. Each table is moved with `ALTER TABLE ... SET SCHEMA <db_id>`, so there are 11 schemas.
   FKs and sequences move with their tables. Gold SQL uses unqualified names and runs unchanged.
3. One read-only role per schema (`bird_ro_<db_id>`): `LOGIN`, `CONNECT` on the database,
   `USAGE` + `SELECT` on its own schema only, `default_transaction_read_only=on`, statement /
   lock / idle-in-transaction timeouts, a connection limit, and `search_path` pinned to the
   schema. `PUBLIC` has no access to the database or to `public`.
4. `ANALYZE` so `pg_class.reltuples` and `pg_stats` are populated for profiling.

Why per-role pinning rather than `SET LOCAL search_path` on one role: a single role that can
switch search_path (or schema-qualify) can read every other database, which breaks the
per-database isolation the safety model relies on. Pools are lazy (`min_size=0`, small
`max_size`) and one per db_id; the container's `max_connections` is shared, so
`concurrency * pools-in-use` must stay well below 100.

## Difficulty counts (from the dataset file)

simple 148, moderate 250, challenging 102 (total 500; 29.6 / 50.0 / 20.4 %).
