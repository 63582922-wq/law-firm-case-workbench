#!/bin/sh
set -eu

umask 077

source_dir=${LAWCASE_POSTGRES_TLS_SOURCE_DIR:-/run/lawcase-postgres-tls}
runtime_dir=${LAWCASE_POSTGRES_TLS_RUNTIME_DIR:-/var/lib/postgresql/lawcase-tls}
hba_file=${LAWCASE_POSTGRES_HBA_FILE:-/etc/postgresql/lawcase-pg_hba.conf}

for path in "$source_dir/ca.crt" "$source_dir/server.crt" "$source_dir/server.key" "$hba_file"; do
    if [ ! -f "$path" ] || [ -L "$path" ]; then
        echo "PostgreSQL TLS/HBA input is missing or unsafe" >&2
        exit 2
    fi
done

install -d -m 0700 -o postgres -g postgres "$runtime_dir"
install -m 0644 -o postgres -g postgres "$source_dir/ca.crt" "$runtime_dir/ca.crt"
install -m 0644 -o postgres -g postgres "$source_dir/server.crt" "$runtime_dir/server.crt"
install -m 0600 -o postgres -g postgres "$source_dir/server.key" "$runtime_dir/server.key"

# The pinned Alpine PostgreSQL image intentionally does not carry the OpenSSL
# CLI. `generate-tls.sh` verifies the chain before mounting it, and PostgreSQL
# itself refuses an unreadable, mismatched, or invalid certificate/key pair
# before listening.

if [ "$#" -eq 0 ]; then
    set -- postgres
fi
if [ "$1" != postgres ]; then
    exec docker-entrypoint.sh "$@"
fi
shift

exec docker-entrypoint.sh postgres \
    -c listen_addresses='*' \
    -c hba_file="$hba_file" \
    -c password_encryption=scram-sha-256 \
    -c ssl=on \
    -c ssl_ca_file="$runtime_dir/ca.crt" \
    -c ssl_cert_file="$runtime_dir/server.crt" \
    -c ssl_key_file="$runtime_dir/server.key" \
    -c ssl_min_protocol_version=TLSv1.2 \
    -c ssl_prefer_server_ciphers=on \
    -c log_connections=on \
    "$@"
