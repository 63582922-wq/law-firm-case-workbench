#!/bin/sh
set -eu

required_secret() {
    name=$1
    eval "value=\${$name-}"
    if [ -z "$value" ] || [ "${#value}" -lt 32 ]; then
        echo "required generated PostgreSQL secret is missing" >&2
        exit 2
    fi
}

for name in \
    LAWCASE_POSTGRES_MIGRATOR_PASSWORD \
    LAWCASE_WEB_APP_POSTGRES_PASSWORD \
    LAWCASE_WEB_IDENTITY_POSTGRES_PASSWORD \
    LAWCASE_WEB_SESSION_POSTGRES_PASSWORD \
    LAWCASE_AGENT_WORKER_POSTGRES_PASSWORD \
    LAWCASE_AGENT_VERIFIER_POSTGRES_PASSWORD \
    LAWCASE_KEYCLOAK_POSTGRES_PASSWORD
do
    required_secret "$name"
done

# psql's \getenv keeps passwords out of command arguments and SQL source.
psql --quiet --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
\getenv migrator_password LAWCASE_POSTGRES_MIGRATOR_PASSWORD
\getenv web_app_password LAWCASE_WEB_APP_POSTGRES_PASSWORD
\getenv identity_password LAWCASE_WEB_IDENTITY_POSTGRES_PASSWORD
\getenv session_password LAWCASE_WEB_SESSION_POSTGRES_PASSWORD
\getenv worker_password LAWCASE_AGENT_WORKER_POSTGRES_PASSWORD
\getenv verifier_password LAWCASE_AGENT_VERIFIER_POSTGRES_PASSWORD
\getenv keycloak_password LAWCASE_KEYCLOAK_POSTGRES_PASSWORD

DO $roles$
DECLARE
    role_name text;
    must_login boolean;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'lawcase_schema_owner',
        'lawcase_ledger_confirmation_owner',
        'lawcase_migrator',
        'lawcase_web_application',
        'lawcase_identity_directory',
        'lawcase_web_session_gateway',
        'lawcase_agent_worker',
        'lawcase_agent_verifier',
        'keycloak'
    ] LOOP
        must_login := role_name NOT IN (
            'lawcase_schema_owner', 'lawcase_ledger_confirmation_owner'
        );
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_roles
            WHERE rolname = role_name
              AND (
                  rolcanlogin IS DISTINCT FROM must_login
                  OR rolinherit
                  OR rolsuper
                  OR rolcreatedb
                  OR rolcreaterole
                  OR rolreplication
                  OR rolbypassrls
              )
        ) THEN
            RAISE EXCEPTION 'unsafe pre-existing role: %', role_name;
        END IF;
    END LOOP;
END
$roles$;

SELECT 'CREATE ROLE lawcase_schema_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lawcase_schema_owner')\gexec
SELECT 'CREATE ROLE lawcase_ledger_confirmation_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lawcase_ledger_confirmation_owner')\gexec

SELECT format(
    'CREATE ROLE lawcase_migrator LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'migrator_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lawcase_migrator')\gexec
SELECT format(
    'CREATE ROLE lawcase_web_application LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'web_app_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lawcase_web_application')\gexec
SELECT format(
    'CREATE ROLE lawcase_identity_directory LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'identity_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lawcase_identity_directory')\gexec
SELECT format(
    'CREATE ROLE lawcase_web_session_gateway LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'session_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lawcase_web_session_gateway')\gexec
SELECT format(
    'CREATE ROLE lawcase_agent_worker LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'worker_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lawcase_agent_worker')\gexec
SELECT format(
    'CREATE ROLE lawcase_agent_verifier LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'verifier_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lawcase_agent_verifier')\gexec
SELECT format(
    'CREATE ROLE keycloak LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'keycloak_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'keycloak')\gexec

ALTER ROLE lawcase_migrator PASSWORD :'migrator_password';
ALTER ROLE lawcase_web_application PASSWORD :'web_app_password';
ALTER ROLE lawcase_identity_directory PASSWORD :'identity_password';
ALTER ROLE lawcase_web_session_gateway PASSWORD :'session_password';
ALTER ROLE lawcase_agent_worker PASSWORD :'worker_password';
ALTER ROLE lawcase_agent_verifier PASSWORD :'verifier_password';
ALTER ROLE keycloak PASSWORD :'keycloak_password';

GRANT lawcase_schema_owner TO lawcase_migrator;
GRANT lawcase_ledger_confirmation_owner TO lawcase_schema_owner;

SELECT 'CREATE DATABASE lawcase OWNER lawcase_schema_owner TEMPLATE template0 ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname = 'lawcase')\gexec
SELECT 'CREATE DATABASE keycloak OWNER keycloak TEMPLATE template0 ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname = 'keycloak')\gexec

REVOKE ALL ON DATABASE lawcase FROM PUBLIC;
GRANT CONNECT ON DATABASE lawcase TO
    lawcase_migrator,
    lawcase_web_application,
    lawcase_identity_directory,
    lawcase_web_session_gateway,
    lawcase_agent_worker,
    lawcase_agent_verifier;

REVOKE ALL ON DATABASE keycloak FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE keycloak TO keycloak;

REVOKE CONNECT ON DATABASE lawcase FROM keycloak;
REVOKE CONNECT ON DATABASE keycloak FROM
    lawcase_migrator,
    lawcase_web_application,
    lawcase_identity_directory,
    lawcase_web_session_gateway,
    lawcase_agent_worker,
    lawcase_agent_verifier;
SQL
