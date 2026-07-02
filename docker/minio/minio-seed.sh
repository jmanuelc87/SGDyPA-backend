#!/bin/sh
set -eu

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

if ! mc ls "local/$MINIO_BUCKET" >/dev/null 2>&1; then
  mc mb --with-lock "local/$MINIO_BUCKET"
fi

mc version enable "local/$MINIO_BUCKET"
mc retention set --default COMPLIANCE 30d "local/$MINIO_BUCKET"
mc anonymous set none "local/$MINIO_BUCKET"

printf 'SGDyPA development bucket with Object Lock enabled.\n' >/tmp/README.txt
mc cp /tmp/README.txt "local/$MINIO_BUCKET/_seed/README.txt"
