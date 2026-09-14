#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/../../.." && pwd)
migrations_dir="$project_root/backend/migrations"
backend_python=${LAWCASE_BACKEND_PYTHON:-$project_root/backend/.venv/bin/python}
run_root=$(mktemp -d)
suffix=$(openssl rand -hex 4)
container_name="lawcase-pg-slice-$suffix"
network_name="lawcase-pg-slice-net-$suffix"
volume_name="lawcase-pg-slice-data-$suffix"

cleanup() {
    docker container rm --force "$container_name" >/dev/null 2>&1 || true
    docker network rm "$network_name" >/dev/null 2>&1 || true
    docker volume rm "$volume_name" >/dev/null 2>&1 || true
    find "$run_root" -depth -delete 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

"$script_dir/generate-tls.sh" "$run_root/tls"
[ -x "$backend_python" ] || {
    echo "backend Python environment is unavailable for application preflight" >&2
    exit 2
}
docker network create "$network_name" >/dev/null
docker volume create "$volume_name" >/dev/null

export POSTGRES_USER=postgres
export POSTGRES_DB=postgres
export POSTGRES_PASSWORD=$(openssl rand -hex 32)
export LAWCASE_POSTGRES_MIGRATOR_PASSWORD=$(openssl rand -hex 32)
export LAWCASE_WEB_APP_POSTGRES_PASSWORD=$(openssl rand -hex 32)
export LAWCASE_WEB_IDENTITY_POSTGRES_PASSWORD=$(openssl rand -hex 32)
export LAWCASE_WEB_SESSION_POSTGRES_PASSWORD=$(openssl rand -hex 32)
export LAWCASE_AGENT_WORKER_POSTGRES_PASSWORD=$(openssl rand -hex 32)
export LAWCASE_AGENT_VERIFIER_POSTGRES_PASSWORD=$(openssl rand -hex 32)
export LAWCASE_KEYCLOAK_POSTGRES_PASSWORD=$(openssl rand -hex 32)
export LAWCASE_TEST_FIRM_ID=11111111-1111-4111-8111-111111111111
export LAWCASE_TEST_LEAD_ACTOR_ID=22222222-2222-4222-8222-222222222222
export LAWCASE_TEST_WORKER_ACTOR_ID=33333333-3333-4333-8333-333333333333
export LAWCASE_TEST_VERIFIER_ACTOR_ID=44444444-4444-4444-8444-444444444444
export LAWCASE_TEST_LEAD_SUBJECT=22222222-2222-4222-8222-222222222222
export LAWCASE_TEST_LEAD_USERNAME=lead.lawyer
export LAWCASE_TEST_FIRM_NAME='Local Managed Acceptance Firm'
export LAWCASE_WEB_OIDC_ISSUER=https://identity.127.0.0.1.nip.io/realms/lawcase-test

docker run --detach \
    --name "$container_name" \
    --network "$network_name" \
    --network-alias postgres \
    --publish 127.0.0.1::5432 \
    --env POSTGRES_USER \
    --env POSTGRES_DB \
    --env POSTGRES_PASSWORD \
    --env LAWCASE_POSTGRES_MIGRATOR_PASSWORD \
    --env LAWCASE_WEB_APP_POSTGRES_PASSWORD \
    --env LAWCASE_WEB_IDENTITY_POSTGRES_PASSWORD \
    --env LAWCASE_WEB_SESSION_POSTGRES_PASSWORD \
    --env LAWCASE_AGENT_WORKER_POSTGRES_PASSWORD \
    --env LAWCASE_AGENT_VERIFIER_POSTGRES_PASSWORD \
    --env LAWCASE_KEYCLOAK_POSTGRES_PASSWORD \
    --volume "$volume_name:/var/lib/postgresql/data" \
    --volume "$script_dir:/slice:ro" \
    --volume "$migrations_dir:/migrations:ro" \
    --volume "$script_dir/initdb:/docker-entrypoint-initdb.d:ro" \
    --volume "$script_dir/pg_hba.conf:/etc/postgresql/lawcase-pg_hba.conf:ro" \
    --volume "$run_root/tls:/run/lawcase-postgres-tls:ro" \
    --entrypoint /slice/postgres-entrypoint.sh \
    postgres:16.14-alpine3.24 >/dev/null

ready=false
attempt=0
while [ "$attempt" -lt 90 ]; do
    if docker logs "$container_name" 2>&1 \
        | grep -q 'PostgreSQL init process complete; ready for start up' \
       && docker exec --user postgres "$container_name" \
          sh -c 'test -S /var/run/postgresql/.s.PGSQL.5432' >/dev/null 2>&1; then
        ready=true
        break
    fi
    if [ "$(docker inspect --format '{{.State.Running}}' "$container_name")" != true ]; then
        docker logs "$container_name"
        exit 2
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$ready" != true ]; then
    docker logs "$container_name"
    exit 2
fi

run_probe() {
    entrypoint=$1
    docker run --rm \
        --network "$network_name" \
        --env LAWCASE_POSTGRES_MIGRATOR_PASSWORD \
        --env LAWCASE_WEB_APP_POSTGRES_PASSWORD \
        --env LAWCASE_WEB_IDENTITY_POSTGRES_PASSWORD \
        --env LAWCASE_WEB_SESSION_POSTGRES_PASSWORD \
        --env LAWCASE_AGENT_WORKER_POSTGRES_PASSWORD \
        --env LAWCASE_AGENT_VERIFIER_POSTGRES_PASSWORD \
        --env LAWCASE_KEYCLOAK_POSTGRES_PASSWORD \
        --env LAWCASE_TEST_FIRM_ID \
        --env LAWCASE_TEST_LEAD_ACTOR_ID \
        --env LAWCASE_TEST_WORKER_ACTOR_ID \
        --env LAWCASE_TEST_VERIFIER_ACTOR_ID \
        --env LAWCASE_TEST_LEAD_SUBJECT \
        --env LAWCASE_TEST_LEAD_USERNAME \
        --env LAWCASE_TEST_FIRM_NAME \
        --env LAWCASE_WEB_OIDC_ISSUER \
        --volume "$script_dir:/slice:ro" \
        --volume "$migrations_dir:/migrations:ro" \
        --volume "$run_root/tls/ca.crt:/run/lawcase-postgres-ca/ca.crt:ro" \
        --entrypoint "$entrypoint" \
        postgres:16.14-alpine3.24
}

docker exec --user postgres \
    --env LAWCASE_MIGRATION_ADMIN_SOCKET=/var/run/postgresql \
    --env LAWCASE_TEST_FIRM_ID \
    --env LAWCASE_TEST_LEAD_ACTOR_ID \
    --env LAWCASE_TEST_WORKER_ACTOR_ID \
    --env LAWCASE_TEST_VERIFIER_ACTOR_ID \
    --env LAWCASE_TEST_LEAD_SUBJECT \
    --env LAWCASE_TEST_LEAD_USERNAME \
    --env LAWCASE_TEST_FIRM_NAME \
    --env LAWCASE_WEB_OIDC_ISSUER \
    "$container_name" /slice/migrate-and-seed.sh
docker exec --user postgres \
    --env LAWCASE_MIGRATION_ADMIN_SOCKET=/var/run/postgresql \
    --env LAWCASE_TEST_FIRM_ID \
    --env LAWCASE_TEST_LEAD_ACTOR_ID \
    --env LAWCASE_TEST_WORKER_ACTOR_ID \
    --env LAWCASE_TEST_VERIFIER_ACTOR_ID \
    --env LAWCASE_TEST_LEAD_SUBJECT \
    --env LAWCASE_TEST_LEAD_USERNAME \
    --env LAWCASE_TEST_FIRM_NAME \
    --env LAWCASE_WEB_OIDC_ISSUER \
    "$container_name" /slice/migrate-and-seed.sh
run_probe /slice/assert-runtime.sh

host_port=$(docker port "$container_name" 5432/tcp | sed -n 's/.*://p' | head -n 1)
if [ -z "$host_port" ]; then
    echo "published PostgreSQL test port is unavailable" >&2
    exit 2
fi
export LAWCASE_PREFLIGHT_WEB_DSN="host=localhost port=$host_port dbname=lawcase user=lawcase_web_application password=$LAWCASE_WEB_APP_POSTGRES_PASSWORD sslmode=verify-full sslrootcert=$run_root/tls/ca.crt"
export LAWCASE_PREFLIGHT_WORKER_DSN="host=localhost port=$host_port dbname=lawcase user=lawcase_agent_worker password=$LAWCASE_AGENT_WORKER_POSTGRES_PASSWORD sslmode=verify-full sslrootcert=$run_root/tls/ca.crt"
export LAWCASE_PREFLIGHT_VERIFIER_DSN="host=localhost port=$host_port dbname=lawcase user=lawcase_agent_verifier password=$LAWCASE_AGENT_VERIFIER_POSTGRES_PASSWORD sslmode=verify-full sslrootcert=$run_root/tls/ca.crt"
PYTHONPATH="$project_root/backend" "$backend_python" "$script_dir/application-preflight.py"

echo "Fresh PostgreSQL container migration and runtime assertions: PASS"
