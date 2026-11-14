"""Download BIRD Mini-Dev (PostgreSQL) and load it into a dedicated Postgres database.

Layout decision (see docs/BIRD_SETUP.md): the official PostgreSQL dump is ONE database whose 75
tables from all 11 BIRD databases sit together in ``public`` (no name collisions across
db_ids). We load the dump as shipped, then move each table into a schema named after its
``db_id`` (``ALTER TABLE ... SET SCHEMA``), create one read-only role per schema whose
``search_path`` is pinned to that schema, and run ANALYZE. Gold SQL uses unqualified table
names, so it runs unchanged under the pinned search_path.

Usage: ``uv run python scripts/bird_setup.py [--skip-download] [--force-reload]``

Environment:
  BIRD_ADMIN_URL      superuser DSN to the maintenance DB (default postgres/postgres@localhost)
  BIRD_DB_NAME        database to create (default bird_minidev)
  BIRD_PG_CONTAINER   docker container used for ``psql`` when no local psql (default nl2sql_postgres)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess  # noqa: S404
import sys
import urllib.request
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "bird_minidev"
RAW_DIR = DATA_DIR / "raw"
ZIP_PATH = DATA_DIR / "_dl" / "minidev.zip"
MANIFEST_PATH = DATA_DIR / "MANIFEST.json"

URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
SOURCE_PAGE = "https://github.com/bird-bench/mini_dev"
WANTED = (
    "minidev/MINIDEV_postgresql/BIRD_dev.sql",
    "minidev/MINIDEV/mini_dev_postgresql.json",
    "minidev/MINIDEV/mini_dev_postgresql_gold.sql",
    "minidev/MINIDEV/dev_tables.json",
)
DUMP = RAW_DIR / "minidev" / "MINIDEV_postgresql" / "BIRD_dev.sql"
QUESTIONS = RAW_DIR / "minidev" / "MINIDEV" / "mini_dev_postgresql.json"
TABLES_JSON = RAW_DIR / "minidev" / "MINIDEV" / "dev_tables.json"

ADMIN_URL = os.environ.get("BIRD_ADMIN_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
DB_NAME = os.environ.get("BIRD_DB_NAME", "bird_minidev")
CONTAINER = os.environ.get("BIRD_PG_CONTAINER", "nl2sql_postgres")
ROLE_PREFIX = "bird_ro_"
ROLE_PASSWORD = os.environ.get("BIRD_RO_PASSWORD", "bird_ro_pass")  # local dev only


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(skip: bool) -> None:
    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists() and ZIP_PATH.stat().st_size > 0:
        print(f"zip already present ({ZIP_PATH.stat().st_size} bytes)")
        return
    if skip:
        sys.exit(f"--skip-download given but {ZIP_PATH} is missing")
    print(f"downloading {URL} (~800 MB) ...")
    if not URL.startswith("https://"):
        sys.exit("refusing non-https URL")
    urllib.request.urlretrieve(URL, ZIP_PATH)  # noqa: S310  (pinned https URL)


def extract() -> None:
    with zipfile.ZipFile(ZIP_PATH) as z:
        for name in WANTED:
            target = RAW_DIR / name
            if target.exists() and target.stat().st_size == z.getinfo(name).file_size:
                continue
            print(f"extracting {name}")
            z.extract(name, RAW_DIR)


def db_url(dbname: str) -> str:
    p = urlparse(ADMIN_URL)
    return urlunparse(p._replace(path=f"/{dbname}"))


def table_owner_map() -> dict[str, str]:
    """lower(table_name) -> db_id, from dev_tables.json (asserts no cross-db collisions)."""
    out: dict[str, str] = {}
    for d in json.loads(TABLES_JSON.read_text()):
        for t in d["table_names_original"]:
            key = t.lower()
            if key in out and out[key] != d["db_id"]:
                raise RuntimeError(f"table {t!r} exists in two databases; layout assumption broken")
            out[key] = d["db_id"]
    return out


def psql_cmd() -> list[str]:
    target = db_url(DB_NAME)
    if shutil.which("psql"):
        return ["psql", target]
    if shutil.which("docker"):
        # connect over the container's own socket as the superuser; DB name is all that matters
        return ["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", DB_NAME]
    sys.exit("need psql or docker to load the dump")


def load_dump() -> None:
    """Stream the dump to psql, dropping OWNER lines (the dump's owner role does not exist)."""
    cmd = [*psql_cmd(), "-q", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-f", "-"]
    print("loading dump (this takes a few minutes) ...")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True)  # noqa: S603
    assert proc.stdin is not None
    with DUMP.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if " OWNER TO " in line and line.lstrip().startswith("ALTER"):
                continue
            proc.stdin.write(line)
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("psql failed while loading the dump")


def public_table_count(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'"
    ).fetchone()
    assert row is not None
    return int(row[0])


def reorganise(conn: psycopg.Connection, owners: dict[str, str]) -> None:
    for db_id in sorted(set(owners.values())):
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(db_id)))
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'"
    ).fetchall()
    for (name,) in rows:
        db_id = owners.get(name.lower())
        if db_id is None:
            raise RuntimeError(f"table {name!r} not in dev_tables.json")
        conn.execute(
            sql.SQL("ALTER TABLE public.{} SET SCHEMA {}").format(
                sql.Identifier(name), sql.Identifier(db_id)
            )
        )


def create_roles(conn: psycopg.Connection, db_ids: list[str]) -> None:
    """One read-only role per schema; same protections as db/setup_role.sql plus search_path."""
    conn.execute(
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(DB_NAME))
    )
    conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    for db_id in db_ids:
        role = sql.Identifier(ROLE_PREFIX + db_id)
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE_PREFIX + db_id,)
        ).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD {} CONNECTION LIMIT 12").format(
                role, sql.Literal(ROLE_PASSWORD)
            )
        )
        schema = sql.Identifier(db_id)
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(DB_NAME), role))
        conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(schema, role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, role))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(schema, role))
        conn.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(
                schema, role
            )
        )
        for key, val in (
            ("default_transaction_read_only", "on"),
            ("statement_timeout", "30000"),
            ("lock_timeout", "5000"),
            ("idle_in_transaction_session_timeout", "30000"),
        ):
            conn.execute(
                sql.SQL("ALTER ROLE {} IN DATABASE {} SET {} = {}").format(
                    role, sql.Identifier(DB_NAME), sql.Identifier(key), sql.Literal(val)
                )
            )
        # search_path pinned to the single schema: no pg_catalog shadowing tricks, no public
        conn.execute(
            sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path = {}").format(
                role, sql.Identifier(DB_NAME), schema
            )
        )


def write_manifest(db_ids: list[str]) -> None:
    questions = json.loads(QUESTIONS.read_text())
    manifest = {
        "dataset": "BIRD Mini-Dev (PostgreSQL dialect)",
        "source_page": SOURCE_PAGE,
        "download_url": URL,
        "zip_bytes": ZIP_PATH.stat().st_size,
        "zip_sha256": sha256_file(ZIP_PATH),
        "files": {
            name: {"bytes": (RAW_DIR / name).stat().st_size, "sha256": sha256_file(RAW_DIR / name)}
            for name in WANTED
        },
        "n_questions": len(questions),
        "difficulty_counts": dict(Counter(q["difficulty"] for q in questions)),
        "db_id_counts": dict(sorted(Counter(q["db_id"] for q in questions).items())),
        "postgres_database": DB_NAME,
        "layout": "one Postgres schema per db_id; one read-only role per schema "
        f"({ROLE_PREFIX}<db_id>) with search_path pinned to that schema",
        "db_ids": db_ids,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    print(f"wrote {MANIFEST_PATH}")
    print("difficulty:", manifest["difficulty_counts"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--force-reload", action="store_true", help="drop and recreate the BIRD DB")
    args = ap.parse_args()

    download(args.skip_download)
    extract()
    owners = table_owner_map()
    db_ids = sorted(set(owners.values()))

    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        if DB_NAME == "nl2sql_assignment":
            sys.exit("refusing to touch nl2sql_assignment")
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,)).fetchone()
        if exists and args.force_reload:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(DB_NAME)))
            exists = None
        if not exists:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))

    with psycopg.connect(db_url(DB_NAME), autocommit=True) as conn:
        schemas = {
            r[0]
            for r in conn.execute(
                "SELECT schema_name FROM information_schema.schemata"
            ).fetchall()
        }
        loaded = set(db_ids) <= schemas and public_table_count(conn) == 0
        if not loaded:
            if public_table_count(conn) == 0:
                load_dump()
            with conn.transaction():
                reorganise(conn, owners)
        else:
            print("database already loaded and reorganised")
        create_roles(conn, db_ids)
        print("ANALYZE ...")
        conn.execute("ANALYZE")
        n = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = ANY(%s)",
            (db_ids,),
        ).fetchone()
        print(f"tables in per-db schemas: {n[0] if n else '?'}")

    write_manifest(db_ids)


if __name__ == "__main__":
    main()
