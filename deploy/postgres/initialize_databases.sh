#!/bin/sh
set -eu
export AGENT_DATABASE_PASSWORD="$(cat "${AGENT_DATABASE_PASSWORD_FILE:-/run/secrets/agent_database_password}")"
export PROFILE_DATABASE_PASSWORD="$(cat "${PROFILE_DATABASE_PASSWORD_FILE:-/run/secrets/profile_database_password}")"
export TELEGRAM_DATABASE_PASSWORD="$(cat "${TELEGRAM_DATABASE_PASSWORD_FILE:-/run/secrets/telegram_database_password}")"
psql -X --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 <<'SQL'
\getenv agent_password AGENT_DATABASE_PASSWORD
\getenv profile_password PROFILE_DATABASE_PASSWORD
\getenv telegram_password TELEGRAM_DATABASE_PASSWORD
CREATE ROLE taskopotamus_agent LOGIN PASSWORD :'agent_password';
CREATE ROLE taskopotamus_profiles LOGIN PASSWORD :'profile_password';
CREATE ROLE taskopotamus_telegram LOGIN PASSWORD :'telegram_password';
CREATE DATABASE taskopotamus_agent OWNER taskopotamus_agent;
CREATE DATABASE taskopotamus_profiles OWNER taskopotamus_profiles;
CREATE DATABASE taskopotamus_telegram OWNER taskopotamus_telegram;
REVOKE CONNECT, TEMPORARY ON DATABASE taskopotamus_agent FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE taskopotamus_profiles FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE taskopotamus_telegram FROM PUBLIC;
SQL
unset AGENT_DATABASE_PASSWORD PROFILE_DATABASE_PASSWORD TELEGRAM_DATABASE_PASSWORD
