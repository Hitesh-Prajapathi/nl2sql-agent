#!/usr/bin/env python
"""Create and (re)seed the dedicated NL red-team database ``nl2sql_redteam``.

Never touches nl2sql_assignment or bird_minidev. Idempotent: every run resets the ``public``
schema of nl2sql_redteam, reloads the demo schema + data, then applies redteam/nl/seed.sql
(poisoned rows, canaries, 300k-row table, read-only role) and, unless --no-hardening, a
name-substituted in-memory copy of db/hardening.sql so the database enforces the same
function/catalog restrictions as the real stack.

Usage:
  PYTHONPATH=$PWD/src uv run python redteam/nl/setup_db.py
Env:  NL2SQL_REDTEAM_SETUP_DSN  superuser DSN of the *server* (default postgres:postgres@localhost)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql as pgsql

ROOT = Path(__file__).resolve().parents[2]
DB_NAME = "nl2sql_redteam"
SERVER_DSN = os.environ.get(
    "NL2SQL_REDTEAM_SETUP_DSN", "postgresql://postgres:postgres@localhost:5432"
)


def hardening_sql() -> str:
    """db/hardening.sql retargeted at the red-team database and role (never written to disk)."""
    text = (ROOT / "db" / "hardening.sql").read_text()
    return text.replace("nl2sql_assignment", DB_NAME).replace("nl2sql_reader", "nl2sql_rt_reader")


def ensure_database(server_dsn: str) -> bool:
    """Create the database if missing. Returns True when it was created."""
    with psycopg.connect(f"{server_dsn}/postgres", autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,)).fetchone()
        if exists:
            return False
        conn.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(DB_NAME)))
        return True


def seed(server_dsn: str, *, hardening: bool = True) -> None:
    created = ensure_database(server_dsn)
    print(f"database {DB_NAME}: {'created' if created else 'already existed (will be reset)'}")
    with psycopg.connect(f"{server_dsn}/{DB_NAME}", autocommit=True) as conn:
        assert conn.execute("SELECT current_database()").fetchone() == (DB_NAME,)  # noqa: S101
        conn.execute("DROP SCHEMA IF EXISTS restricted CASCADE")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for rel in ("db/schema.sql", "db/seed_data.sql", "redteam/nl/seed.sql"):
            conn.execute((ROOT / rel).read_text())  # type: ignore[arg-type]
            print(f"applied {rel}")
        if hardening:
            conn.execute(hardening_sql())  # type: ignore[arg-type]
            print("applied db/hardening.sql (retargeted)")
    print(
        "done. DSN for the agent: postgresql://nl2sql_rt_reader:rt_reader_pass@localhost:5432/"
        + DB_NAME
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--no-hardening", action="store_true", help="skip the db/hardening.sql copy")
    ap.add_argument("--server-dsn", default=SERVER_DSN)
    args = ap.parse_args()
    seed(args.server_dsn, hardening=not args.no_hardening)
    return 0


if __name__ == "__main__":
    sys.exit(main())
