#!/bin/sh
set -eu

alias_name=lawcase
endpoint=https://object-storage:9000

until mc alias set "$alias_name" "$endpoint" "$LAWCASE_OBJECT_ROOT_USER" "$LAWCASE_OBJECT_ROOT_PASSWORD" >/dev/null 2>&1; do
  sleep 2
done

mc mb --ignore-existing "$alias_name/$LAWCASE_OBJECT_BUCKET"
mc version enable "$alias_name/$LAWCASE_OBJECT_BUCKET"
mc anonymous set none "$alias_name/$LAWCASE_OBJECT_BUCKET"

if [ "$LAWCASE_OBJECT_BUCKET" != lawcase-private-alpha ]; then
  echo "managed object bucket differs from the fixed policy contract" >&2
  exit 2
fi

mc admin policy create "$alias_name" lawcase-web /policies/web-policy.json >/dev/null 2>&1 || \
  mc admin policy info "$alias_name" lawcase-web >/dev/null
mc admin policy create "$alias_name" lawcase-worker /policies/worker-policy.json >/dev/null 2>&1 || \
  mc admin policy info "$alias_name" lawcase-worker >/dev/null

mc admin user add "$alias_name" "$LAWCASE_WEB_OBJECT_ACCESS_KEY_ID" "$LAWCASE_WEB_OBJECT_SECRET_ACCESS_KEY" >/dev/null 2>&1 || \
  mc admin user enable "$alias_name" "$LAWCASE_WEB_OBJECT_ACCESS_KEY_ID" >/dev/null
mc admin user add "$alias_name" "$LAWCASE_WORKER_OBJECT_ACCESS_KEY_ID" "$LAWCASE_WORKER_OBJECT_SECRET_ACCESS_KEY" >/dev/null 2>&1 || \
  mc admin user enable "$alias_name" "$LAWCASE_WORKER_OBJECT_ACCESS_KEY_ID" >/dev/null
mc admin policy attach "$alias_name" lawcase-web --user "$LAWCASE_WEB_OBJECT_ACCESS_KEY_ID" >/dev/null
mc admin policy attach "$alias_name" lawcase-worker --user "$LAWCASE_WORKER_OBJECT_ACCESS_KEY_ID" >/dev/null

# A successful bootstrap means the bucket is private, versioned and reachable;
# the separate boto3 probe proves both least-privilege identities can perform
# the exact AES256 PUT/HEAD/GET/DELETE contract used by the application.
mc stat "$alias_name/$LAWCASE_OBJECT_BUCKET" >/dev/null
