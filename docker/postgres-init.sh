#!/bin/bash
# First-start initialisation of the TrustRAG database (runs once, on an empty data volume, as the superuser).
# Why: the pgvector extension needs a superuser to be created, but the application must NOT run as one. So this script
# creates the extension and a least-privilege role `trustrag_app` that owns only the schema `trustrag`.
# The app password is read from a mounted secret file by psql itself, so it is never on a command line or in the image.
set -euo pipefail

APP_PASSWORD_FILE="${APP_PASSWORD_FILE:-/run/secrets/pg_app_password}"
[ -r "$APP_PASSWORD_FILE" ] || { echo "postgres-init: app password file not readable" >&2; exit 1; }

export APP_PASSWORD_FILE
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
-- Keep the plaintext password out of error output and the server log if CREATE ROLE ever fails.
\set VERBOSITY terse
SET log_min_error_statement = panic;
SET log_statement = 'none';
SET log_min_duration_statement = -1;
\set apppw `cat "$APP_PASSWORD_FILE"`
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE ROLE trustrag_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'apppw';
\unset apppw
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA trustrag AUTHORIZATION trustrag_app;
SQL
