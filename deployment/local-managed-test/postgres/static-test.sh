#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/../../.." && pwd)
migrations_dir="$project_root/backend/migrations"

for script in \
    "$script_dir/generate-tls.sh" \
    "$script_dir/postgres-entrypoint.sh" \
    "$script_dir/initdb/010-roles-and-databases.sh" \
    "$script_dir/migrate-and-seed.sh" \
    "$script_dir/assert-runtime.sh" \
    "$script_dir/container-test.sh"
do
    sh -n "$script"
done

python_bin=${LAWCASE_BACKEND_PYTHON:-$project_root/backend/.venv/bin/python}
if [ ! -x "$python_bin" ]; then
    echo "backend Python environment is unavailable for application preflight" >&2
    exit 2
fi
"$python_bin" -c \
    'import pathlib, sys; compile(pathlib.Path(sys.argv[1]).read_text(), sys.argv[1], "exec")' \
    "$script_dir/application-preflight.py"

grep -Eq '^hostnossl[[:space:]]+all[[:space:]]+all.*reject$' "$script_dir/pg_hba.conf"
if grep -Eq '^host[[:space:]]+all[[:space:]]+all' "$script_dir/pg_hba.conf"; then
    echo "pg_hba.conf contains a non-TLS catch-all" >&2
    exit 2
fi
for role in \
    lawcase_web_application \
    lawcase_identity_directory \
    lawcase_web_session_gateway \
    lawcase_agent_worker \
    lawcase_agent_verifier
do
    grep -Eq "^hostssl[[:space:]]+lawcase[[:space:]]+$role[[:space:]]" "$script_dir/pg_hba.conf"
done

expected=1
for migration_path in $(find "$migrations_dir" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9]_*.sql' | LC_ALL=C sort); do
    filename=$(basename "$migration_path")
    prefix=${filename%%_*}
    number=$(printf '%s' "$prefix" | sed 's/^0*//')
    [ -n "$number" ] || number=0
    if [ "$number" -ne "$expected" ]; then
        echo "backend migration sequence is not continuous" >&2
        exit 2
    fi
    expected=$((expected + 1))
done
if [ "$expected" -le 1 ]; then
    echo "backend migration sequence is empty" >&2
    exit 2
fi

tmp_root=$(mktemp -d)
trap 'rm -rf "$tmp_root"' EXIT HUP INT TERM
"$script_dir/generate-tls.sh" "$tmp_root/tls"
"$script_dir/generate-tls.sh" "$tmp_root/tls"
openssl verify -CAfile "$tmp_root/tls/ca.crt" "$tmp_root/tls/server.crt" >/dev/null
openssl x509 -in "$tmp_root/tls/server.crt" -noout -text | grep -q 'DNS:postgres'

mode_of() {
    if stat -f '%Lp' "$1" >/dev/null 2>&1; then
        stat -f '%Lp' "$1"
    else
        stat -c '%a' "$1"
    fi
}

[ "$(mode_of "$tmp_root/tls/ca.key")" = 600 ]
[ "$(mode_of "$tmp_root/tls/server.key")" = 600 ]
[ "$(mode_of "$tmp_root/tls")" = 700 ]

if grep -R -E \
    '(LAWCASE_.*PASSWORD=.{32,}|postgresql://[^:]+:[^@[:space:]]{16,}@)' \
    "$script_dir" --exclude='static-test.sh' >/dev/null 2>&1; then
    echo "tracked PostgreSQL slice appears to contain a fixed secret" >&2
    exit 2
fi

echo "PostgreSQL static contract and generated TLS material: PASS"
