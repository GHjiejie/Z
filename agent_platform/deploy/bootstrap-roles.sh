#!/bin/sh
set -eu

# Runs only when a new PostgreSQL data directory is initialized. Existing
# installations must apply an explicit owner/role migration before SaaS startup.
# Read credentials inside psql: passwords never become shell/psql command args.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<'SQL'
\getenv migration_password PLATFORM_MIGRATION_DB_PASSWORD
\getenv runtime_password PLATFORM_DB_PASSWORD
\getenv litellm_password LITELLM_DB_PASSWORD
SELECT 'CREATE ROLE platform_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='platform_migrator') \gexec
SELECT 'CREATE ROLE platform_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='platform_runtime') \gexec
SELECT 'CREATE ROLE litellm LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='litellm') \gexec
SELECT format('ALTER ROLE platform_migrator PASSWORD %L', :'migration_password') \gexec
SELECT format('ALTER ROLE platform_runtime PASSWORD %L', :'runtime_password') \gexec
SELECT format('ALTER ROLE litellm PASSWORD %L', :'litellm_password') \gexec
ALTER ROLE platform_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
SELECT 'CREATE DATABASE platform OWNER platform_migrator'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='platform') \gexec
SELECT 'CREATE DATABASE litellm OWNER litellm'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='litellm') \gexec
REVOKE CONNECT ON DATABASE platform FROM PUBLIC;
REVOKE CONNECT ON DATABASE litellm FROM PUBLIC;
GRANT CONNECT ON DATABASE platform TO platform_migrator, platform_runtime;
GRANT CONNECT ON DATABASE litellm TO litellm;
\connect platform
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO platform_migrator;
GRANT USAGE ON SCHEMA public TO platform_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO platform_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO platform_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO platform_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO platform_runtime;
SQL
