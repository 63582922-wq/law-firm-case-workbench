#!/bin/sh
set -eu

umask 077

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_dir=${1:-}
if [ -z "$target_dir" ] || [ "${target_dir#/}" = "$target_dir" ]; then
    echo "generate-tls.sh requires an absolute target directory" >&2
    exit 2
fi

ca_key="$target_dir/ca.key"
ca_cert="$target_dir/ca.crt"
server_key="$target_dir/server.key"
server_csr="$target_dir/server.csr"
server_cert="$target_dir/server.crt"
serial_file="$target_dir/ca.srl"

present=0
for path in "$ca_key" "$ca_cert" "$server_key" "$server_cert"; do
    if [ -e "$path" ]; then
        present=$((present + 1))
    fi
done

if [ "$present" -ne 0 ] && [ "$present" -ne 4 ]; then
    echo "partial PostgreSQL TLS material exists; refusing implicit repair" >&2
    exit 2
fi

mkdir -p "$target_dir"
chmod 700 "$target_dir"

if [ "$present" -eq 0 ]; then
    openssl genrsa -out "$ca_key" 3072 >/dev/null 2>&1
    openssl req -x509 -new -sha256 -days 825 \
        -key "$ca_key" \
        -subj "/CN=Lawcase Local Managed PostgreSQL Root CA/O=Lawcase Local Managed Test" \
        -out "$ca_cert"
    openssl genrsa -out "$server_key" 3072 >/dev/null 2>&1
    openssl req -new -sha256 \
        -key "$server_key" \
        -config "$script_dir/openssl.cnf" \
        -out "$server_csr"
    openssl x509 -req -sha256 -days 825 \
        -in "$server_csr" \
        -CA "$ca_cert" \
        -CAkey "$ca_key" \
        -CAcreateserial \
        -extfile "$script_dir/openssl.cnf" \
        -extensions server_extensions \
        -out "$server_cert" >/dev/null 2>&1
    rm -f "$server_csr" "$serial_file"
fi

chmod 600 "$ca_key" "$server_key"
chmod 644 "$ca_cert" "$server_cert"

openssl rsa -check -noout -in "$ca_key" >/dev/null 2>&1
openssl rsa -check -noout -in "$server_key" >/dev/null 2>&1
openssl verify -CAfile "$ca_cert" "$server_cert" >/dev/null
openssl x509 -in "$server_cert" -noout -text | grep -q 'DNS:postgres'

