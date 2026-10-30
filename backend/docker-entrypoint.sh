#!/bin/sh
set -e

mkdir -p "$STORAGE_ROOT" "$(dirname "$SQLITE_PATH")"
python manage.py check --tag gsr_dtw_c --fail-level ERROR
python manage.py migrate --noinput

exec "$@"
