#!/bin/sh
set -eu

postgres_host=${LAWCASE_POSTGRES_HOST:-postgres}
postgres_port=${LAWCASE_POSTGRES_PORT:-5432}
ca_file=${LAWCASE_POSTGRES_CA_FILE:-/run/lawcase-postgres-ca/ca.crt}
migrations_dir=${LAWCASE_MIGRATIONS_DIR:-/migrations}

required() {
    name=$1
    eval "value=\${$name-}"
    if [ -z "$value" ]; then
        echo "managed PostgreSQL assertion setting is missing" >&2
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
    LAWCASE_KEYCLOAK_POSTGRES_PASSWORD \
    LAWCASE_TEST_FIRM_ID \
    LAWCASE_TEST_LEAD_ACTOR_ID \
    LAWCASE_TEST_WORKER_ACTOR_ID \
    LAWCASE_TEST_VERIFIER_ACTOR_ID \
    LAWCASE_TEST_LEAD_SUBJECT \
    LAWCASE_WEB_OIDC_ISSUER
do
    required "$name"
done

if [ ! -r "$ca_file" ] || [ ! -d "$migrations_dir" ]; then
    echo "managed PostgreSQL assertion inputs are unavailable" >&2
    exit 2
fi

psql_as() {
    role=$1
    password=$2
    database=$3
    shift 3
    PGPASSWORD=$password psql -X --quiet --set ON_ERROR_STOP=1 \
        "host=$postgres_host port=$postgres_port dbname=$database user=$role sslmode=verify-full sslrootcert=$ca_file" \
        "$@"
}

migration_count=$(find "$migrations_dir" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9]_*.sql' | wc -l | tr -d ' ')

psql_as lawcase_migrator "$LAWCASE_POSTGRES_MIGRATOR_PASSWORD" lawcase \
    --set expected_migration_count="$migration_count" \
    --set firm_id="$LAWCASE_TEST_FIRM_ID" \
    --set lead_actor_id="$LAWCASE_TEST_LEAD_ACTOR_ID" \
    --set worker_actor_id="$LAWCASE_TEST_WORKER_ACTOR_ID" \
    --set verifier_actor_id="$LAWCASE_TEST_VERIFIER_ACTOR_ID" \
    --set lead_subject="$LAWCASE_TEST_LEAD_SUBJECT" \
    --set oidc_issuer="$LAWCASE_WEB_OIDC_ISSUER" <<'SQL'
SELECT set_config(
    'lawcase_assert.tls_enabled',
    COALESCE((
        SELECT ssl::text FROM pg_catalog.pg_stat_ssl
        WHERE pid = pg_backend_pid()
    ), 'false'),
    false
);
SELECT set_config(
    'lawcase_assert.tls_version',
    COALESCE((
        SELECT version FROM pg_catalog.pg_stat_ssl
        WHERE pid = pg_backend_pid()
    ), 'NONE'),
    false
);
SET ROLE lawcase_schema_owner;
SELECT set_config('app.firm_id', :'firm_id', false);
SELECT set_config(
    'lawcase_assert.expected_migration_count',
    :'expected_migration_count', false
);
SELECT set_config('lawcase_assert.firm_id', :'firm_id', false);
SELECT set_config('lawcase_assert.lead_actor_id', :'lead_actor_id', false);
SELECT set_config('lawcase_assert.worker_actor_id', :'worker_actor_id', false);
SELECT set_config('lawcase_assert.verifier_actor_id', :'verifier_actor_id', false);
SELECT set_config('lawcase_assert.lead_subject', :'lead_subject', false);
SELECT set_config('lawcase_assert.oidc_issuer', :'oidc_issuer', false);
DO $assert$
DECLARE
    invalid_count integer;
BEGIN
    IF current_setting('server_version_num')::integer < 160000
       OR current_setting('server_version_num')::integer >= 170000 THEN
        RAISE EXCEPTION 'expected PostgreSQL 16.x';
    END IF;
    IF current_setting('lawcase_assert.tls_enabled')::boolean IS DISTINCT FROM TRUE
       OR current_setting('lawcase_assert.tls_version')
          NOT IN ('TLSv1.2', 'TLSv1.3') THEN
        RAISE EXCEPTION 'assertion connection is not TLS 1.2+';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.lawcase_schema_migrations
    WHERE state <> 'APPLIED' OR applied_at IS NULL;
    IF invalid_count <> 0 OR (
        SELECT count(*) FROM public.lawcase_schema_migrations
    ) <> current_setting(
        'lawcase_assert.expected_migration_count'
    )::integer THEN
        RAISE EXCEPTION 'migration ledger is incomplete';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM pg_catalog.pg_roles
    WHERE rolname IN (
        'lawcase_schema_owner', 'lawcase_ledger_confirmation_owner'
    ) AND (
        rolcanlogin OR rolinherit OR rolsuper OR rolcreatedb OR rolcreaterole
        OR rolreplication OR rolbypassrls
    );
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'NOLOGIN owner role attributes are unsafe';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM pg_catalog.pg_roles
    WHERE rolname IN (
        'lawcase_migrator', 'lawcase_web_application',
        'lawcase_identity_directory', 'lawcase_web_session_gateway',
        'lawcase_agent_worker', 'lawcase_agent_verifier', 'keycloak'
    ) AND (
        NOT rolcanlogin OR rolinherit OR rolsuper OR rolcreatedb OR rolcreaterole
        OR rolreplication OR rolbypassrls
    );
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'LOGIN role attributes are unsafe';
    END IF;

    IF has_database_privilege('keycloak', 'lawcase', 'CONNECT')
       OR has_database_privilege('lawcase_web_application', 'keycloak', 'CONNECT')
       OR has_schema_privilege('lawcase_web_application', 'public', 'CREATE')
       OR has_schema_privilege('lawcase_agent_worker', 'public', 'CREATE')
       OR has_schema_privilege('lawcase_agent_verifier', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'database/schema isolation is broader than declared';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM pg_catalog.pg_class relation
    JOIN pg_catalog.pg_namespace namespace ON namespace.oid = relation.relnamespace
    JOIN pg_catalog.pg_attribute attribute ON attribute.attrelid = relation.oid
    WHERE namespace.nspname = 'public'
      AND relation.relkind IN ('r', 'p')
      AND relation.relname NOT IN ('firms', 'web_oidc_identities')
      AND attribute.attname = 'firm_id'
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND (NOT relation.relrowsecurity OR NOT relation.relforcerowsecurity);
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'a tenant table is missing FORCE RLS';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public.web_oidc_identities
        WHERE issuer = current_setting('lawcase_assert.oidc_issuer')
          AND subject = current_setting('lawcase_assert.lead_subject')
          AND firm_id = current_setting('lawcase_assert.firm_id')::uuid
          AND user_id = current_setting('lawcase_assert.lead_actor_id')::uuid
          AND is_active AND active_human_roles = ARRAY['LEAD_LAWYER']::text[]
    ) OR NOT EXISTS (
        SELECT 1 FROM public.users
        WHERE firm_id = current_setting('lawcase_assert.firm_id')::uuid
          AND user_id = current_setting(
              'lawcase_assert.worker_actor_id'
          )::uuid AND status = 'ACTIVE'
    ) OR NOT EXISTS (
        SELECT 1 FROM public.users
        WHERE firm_id = current_setting('lawcase_assert.firm_id')::uuid
          AND user_id = current_setting(
              'lawcase_assert.verifier_actor_id'
          )::uuid AND status = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION 'deterministic managed-test principals are incomplete';
    END IF;
END
$assert$;
SQL

# Prove every production DSN is a distinct TLS-authenticated current_user.
printf '%s\n' \
    "SELECT set_config('app.firm_id', :'firm_id', false);" \
    "SELECT current_user FROM public.users WHERE firm_id = :'firm_id'::uuid LIMIT 1;" \
    | psql_as lawcase_web_application "$LAWCASE_WEB_APP_POSTGRES_PASSWORD" lawcase \
        --set firm_id="$LAWCASE_TEST_FIRM_ID" --tuples-only --no-align \
    | grep -q '^lawcase_web_application$'
printf '%s\n' \
    "SELECT current_user FROM public.web_oidc_identities WHERE issuer = :'oidc_issuer' AND subject = :'lead_subject';" \
    | psql_as lawcase_identity_directory "$LAWCASE_WEB_IDENTITY_POSTGRES_PASSWORD" lawcase \
        --set oidc_issuer="$LAWCASE_WEB_OIDC_ISSUER" \
        --set lead_subject="$LAWCASE_TEST_LEAD_SUBJECT" \
        --tuples-only --no-align \
    | grep -q '^lawcase_identity_directory$'
psql_as lawcase_web_session_gateway "$LAWCASE_WEB_SESSION_POSTGRES_PASSWORD" lawcase \
    --tuples-only --no-align --command "SELECT current_user" \
    | grep -q '^lawcase_web_session_gateway$'
printf '%s\n' \
    "SELECT set_config('app.firm_id', :'firm_id', false);" \
    "SELECT current_user FROM public.users WHERE firm_id = :'firm_id'::uuid LIMIT 1;" \
    | psql_as lawcase_agent_worker "$LAWCASE_AGENT_WORKER_POSTGRES_PASSWORD" lawcase \
        --set firm_id="$LAWCASE_TEST_FIRM_ID" --tuples-only --no-align \
    | grep -q '^lawcase_agent_worker$'
printf '%s\n' \
    "SELECT set_config('app.firm_id', :'firm_id', false);" \
    "SELECT current_user FROM public.users WHERE firm_id = :'firm_id'::uuid LIMIT 1;" \
    | psql_as lawcase_agent_verifier "$LAWCASE_AGENT_VERIFIER_POSTGRES_PASSWORD" lawcase \
        --set firm_id="$LAWCASE_TEST_FIRM_ID" --tuples-only --no-align \
    | grep -q '^lawcase_agent_verifier$'
psql_as keycloak "$LAWCASE_KEYCLOAK_POSTGRES_PASSWORD" keycloak \
    --tuples-only --no-align --command "SELECT current_user" \
    | grep -q '^keycloak$'

# A non-TLS connection must be rejected even with a valid password.
if PGPASSWORD=$LAWCASE_WEB_APP_POSTGRES_PASSWORD psql -X --quiet \
    "host=$postgres_host port=$postgres_port dbname=lawcase user=lawcase_web_application sslmode=disable" \
    --command 'SELECT 1' >/dev/null 2>&1; then
    echo "PostgreSQL accepted a non-TLS runtime connection" >&2
    exit 2
fi

# Global identity/session tables are not ordinary application/Worker reads.
if psql_as lawcase_web_application "$LAWCASE_WEB_APP_POSTGRES_PASSWORD" lawcase \
    --command 'SELECT 1 FROM public.web_oidc_identities LIMIT 1' >/dev/null 2>&1; then
    echo "Web application can enumerate the OIDC directory" >&2
    exit 2
fi
if psql_as lawcase_agent_worker "$LAWCASE_AGENT_WORKER_POSTGRES_PASSWORD" lawcase \
    --command 'SELECT 1 FROM public.web_sessions LIMIT 1' >/dev/null 2>&1; then
    echo "Agent Worker can enumerate Web sessions" >&2
    exit 2
fi

echo "PostgreSQL 16 TLS, migrations, roles, RLS, seed and Keycloak isolation: PASS"
