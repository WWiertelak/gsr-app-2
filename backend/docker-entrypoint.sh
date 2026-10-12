#!/bin/sh
set -e

mkdir -p "$STORAGE_ROOT" "$(dirname "$SQLITE_PATH")"
python manage.py migrate --noinput

# Optional admin account from DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_PASSWORD
# (and DJANGO_SUPERUSER_EMAIL). Created once; an existing user is left unchanged.
if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    python manage.py shell -c "
import os
from django.contrib.auth import get_user_model
User = get_user_model()
username = os.environ['DJANGO_SUPERUSER_USERNAME']
if User.objects.filter(username=username).exists():
    print(f'Admin user {username!r} already exists, leaving it unchanged.')
else:
    User.objects.create_superuser(
        username, os.environ.get('DJANGO_SUPERUSER_EMAIL', ''), os.environ['DJANGO_SUPERUSER_PASSWORD']
    )
    print(f'Created admin user {username!r}.')
"
fi

exec "$@"
