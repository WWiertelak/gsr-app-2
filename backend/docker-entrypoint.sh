#!/bin/sh
set -e

mkdir -p "$STORAGE_ROOT" "$(dirname "$SQLITE_PATH")"
python manage.py check --fail-level ERROR
python manage.py migrate --noinput

exec "$@"
