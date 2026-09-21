#!/bin/sh
set -eu

umask 077

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
migrations_dir=${LAWCASE_MIGRATIONS_DIR:-/migrations}
admin_socket=${LAWCASE_MIGRATION_ADMIN_SOCKET:-}

required() {
    name=$1
    eval "value=\${$name-}"
    if [ -z "$value" ]; then
        echo "managed PostgreSQL migration setting is missing" >&2
        exit 2
    fi
}

required LAWCASE_TEST_FIRM_ID
required LAWCASE_TEST_FIRM_NAME
required LAWCASE_TEST_LEAD_ACTOR_ID
required LAWCASE_TEST_WORKER_ACTOR_ID
required LAWCASE_TEST_VERIFIER_ACTOR_ID
required LAWCASE_TEST_LEAD_SUBJECT
required LAWCASE_TEST_LEAD_USERNAME
required LAWCASE_WEB_OIDC_ISSUER

if [ -z "$admin_socket" ] || [ "${admin_socket#/}" = "$admin_socket" ] \
   || [ ! -S "$admin_socket/.s.PGSQL.5432" ] || [ ! -d "$migrations_dir" ]; then
    echo "managed PostgreSQL migration inputs are unavailable" >&2
    exit 2
fi

export PGHOST="$admin_socket"
export PGPORT=5432
export PGDATABASE=lawcase
export PGUSER=postgres
unset PGPASSWORD PGSSLMODE PGSSLROOTCERT

admin_window_open=false
close_admin_window() {
    if [ "$admin_window_open" = true ]; then
        psql -X --quiet --set ON_ERROR_STOP=1 <<'SQL' >/dev/null
ALTER ROLE lawcase_schema_owner NOSUPERUSER;
GRANT lawcase_schema_owner TO lawcase_migrator;
REVOKE CREATE ON SCHEMA public FROM lawcase_ledger_confirmation_owner;
SQL
        admin_window_open=false
    fi
}
cleanup() {
    status=$?
    close_admin_window || true
    if [ -n "${list_file:-}" ]; then
        rm -f "$list_file"
    fi
    trap - EXIT HUP INT TERM
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

attempt=0
until psql -X --quiet --set ON_ERROR_STOP=1 --command \
    "SELECT 1 FROM pg_catalog.pg_database WHERE datname = current_database()" \
    >/dev/null 2>&1
do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        echo "PostgreSQL did not become TLS-ready within the bounded wait" >&2
        exit 2
    fi
    sleep 1
done

actual_session_user=$(psql -X --quiet --tuples-only --no-align \
    --set ON_ERROR_STOP=1 --command 'SELECT session_user')
if [ "$actual_session_user" != postgres ]; then
    echo "migration job requires the local PostgreSQL bootstrap principal" >&2
    exit 2
fi

# The source migrations intentionally transfer ownership after revoking the
# target owner's schema CREATE bit. A normal role cannot finish that same file.
# Remove the only login membership before the bounded elevation; this leaves a
# SIGKILL-interrupted NOLOGIN super-role unreachable until an administrator
# inspects the retained APPLYING marker.
psql -X --quiet --set ON_ERROR_STOP=1 <<'SQL'
REVOKE lawcase_schema_owner FROM lawcase_migrator;
ALTER ROLE lawcase_schema_owner SUPERUSER;
SQL
admin_window_open=true

psql -X --quiet --set ON_ERROR_STOP=1 --file "$script_dir/prepare-schema.sql"

list_file=$(mktemp)
find "$migrations_dir" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9]_*.sql' -print \
    | LC_ALL=C sort > "$list_file"

if [ ! -s "$list_file" ]; then
    echo "no numbered PostgreSQL migrations were found" >&2
    exit 2
fi

expected=1
while IFS= read -r migration_path; do
    filename=$(basename "$migration_path")
    case "$filename" in
        [0-9][0-9][0-9][0-9]_[a-z0-9_]*.sql) ;;
        *)
            echo "migration filename is outside the governed pattern" >&2
            exit 2
            ;;
    esac
    prefix=${filename%%_*}
    number=$(printf '%s' "$prefix" | sed 's/^0*//')
    [ -n "$number" ] || number=0
    if [ "$number" -ne "$expected" ]; then
        echo "PostgreSQL migration sequence is not continuous" >&2
        exit 2
    fi
    expected=$((expected + 1))

    source_sha256=$(sha256sum "$migration_path" | awk '{print $1}')
    observed=$(psql -X --quiet --tuples-only --no-align --field-separator='|' \
        --set ON_ERROR_STOP=1 \
        --command "SET ROLE lawcase_schema_owner; SELECT filename, source_sha256, state FROM public.lawcase_schema_migrations WHERE migration_number = $number")
    if [ -n "$observed" ]; then
        if [ "$observed" != "$filename|$source_sha256|APPLIED" ]; then
            echo "migration history is interrupted or differs from source" >&2
            exit 2
        fi
        continue
    fi

    psql -X --quiet --set ON_ERROR_STOP=1 \
        --set migration_number="$number" \
        --set filename="$filename" \
        --set source_sha256="$source_sha256" <<'SQL'
SET ROLE lawcase_schema_owner;
INSERT INTO public.lawcase_schema_migrations (
    migration_number, filename, source_sha256, state
) VALUES (
    :'migration_number'::integer, :'filename', :'source_sha256', 'APPLYING'
);
SQL

    if ! {
        printf '%s\n' 'SET ROLE lawcase_schema_owner;'
        # These immutable migrations explicitly SET LOCAL ROLE to the definer
        # owner before replacing its functions. Earlier migrations revoke its
        # schema CREATE. Restore only for these files, never for runtime roles.
        case "$number" in
            64|66|67)
                printf '%s\n' 'GRANT CREATE ON SCHEMA public TO lawcase_ledger_confirmation_owner;'
                ;;
        esac
        printf '\\i %s\n' "$migration_path"
        printf '%s\n' 'REVOKE CREATE ON SCHEMA public FROM lawcase_ledger_confirmation_owner;'
    } | psql -X --quiet --set ON_ERROR_STOP=1; then
        echo "PostgreSQL migration failed; APPLYING marker retained" >&2
        exit 2
    fi

    psql -X --quiet --set ON_ERROR_STOP=1 \
        --set migration_number="$number" \
        --set filename="$filename" \
        --set source_sha256="$source_sha256" <<'SQL'
SET ROLE lawcase_schema_owner;
UPDATE public.lawcase_schema_migrations
SET state = 'APPLIED', applied_at = clock_timestamp()
WHERE migration_number = :'migration_number'::integer
  AND filename = :'filename'
  AND source_sha256 = :'source_sha256'
  AND state = 'APPLYING';
\if :ROW_COUNT
\else
\echo 'migration APPLIED marker update did not affect exactly one row'
\quit 3
\endif
SQL
done < "$list_file"

psql -X --quiet --set ON_ERROR_STOP=1 --file "$script_dir/post-migrate-hardening.sql"
close_admin_window

case "$LAWCASE_WEB_OIDC_ISSUER" in
    https://*) ;;
    *)
        echo "managed OIDC issuer must be HTTPS" >&2
        exit 2
        ;;
esac

psql -X --quiet --set ON_ERROR_STOP=1 \
    --set firm_id="$LAWCASE_TEST_FIRM_ID" \
    --set firm_name="$LAWCASE_TEST_FIRM_NAME" \
    --set lead_actor_id="$LAWCASE_TEST_LEAD_ACTOR_ID" \
    --set worker_actor_id="$LAWCASE_TEST_WORKER_ACTOR_ID" \
    --set verifier_actor_id="$LAWCASE_TEST_VERIFIER_ACTOR_ID" \
    --set lead_subject="$LAWCASE_TEST_LEAD_SUBJECT" \
    --set lead_username="$LAWCASE_TEST_LEAD_USERNAME" \
    --set oidc_issuer="$LAWCASE_WEB_OIDC_ISSUER" \
    --file "$script_dir/seed.sql"
