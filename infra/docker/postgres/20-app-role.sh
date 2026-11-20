#!/bin/sh
# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.
#
# Runs once on a FRESH data volume (docker-entrypoint-initdb.d), as the
# bootstrap superuser. Creates the least-privilege runtime role (ADR-0026).
# No table grants here: tables do not exist yet, and a new table must get NO
# access until `python -m src.app db-bootstrap` re-applies the exact grants
# after migrations (default-deny; deliberately no ALTER DEFAULT PRIVILEGES).
# The password comes from the container environment and is quoted by the server
# (format %L); it is never written to a file or a command line.
set -eu

role="${NIMBUS_APP_ROLE:-nimbus_app}"
pw="${NIMBUS_APP_PASSWORD:-}"
if [ -z "$pw" ]; then
    echo "20-app-role: NIMBUS_APP_PASSWORD empty; skipping (run db-bootstrap later)"
    exit 0
fi

psql -v ON_ERROR_STOP=1 -v role="$role" -v pw="$pw" -v db="$POSTGRES_DB" \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L',
    :'role', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'role') \gexec
SELECT format('REVOKE CREATE ON SCHEMA public FROM %I', :'role') \gexec
SQL
