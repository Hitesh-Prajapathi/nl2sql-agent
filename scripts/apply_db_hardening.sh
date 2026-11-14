#!/usr/bin/env bash
# Apply db/hardening.sql to the nl2sql_assignment database as a superuser.
# Idempotent. Uses local psql when available, else the docker-compose container.
set -euo pipefail
cd "$(dirname "$0")/.."
FILE=db/hardening.sql
DB="${PGDATABASE:-nl2sql_assignment}"
if command -v psql >/dev/null 2>&1; then
  PGUSER="${PGUSER:-postgres}" PGPASSWORD="${PGPASSWORD:-postgres}" \
    PGHOST="${PGHOST:-localhost}" \
    psql -v ON_ERROR_STOP=1 -d "$DB" -f "$FILE"
else
  docker exec -i nl2sql_postgres psql -v ON_ERROR_STOP=1 -U postgres -d "$DB" < "$FILE"
fi
