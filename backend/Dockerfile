FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Pango (with HarfBuzz and fontconfig) for WeasyPrint, the report's HTML-to-PDF converter.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Test image: adds pytest, ruff and the legacy GSRv2 reference dependencies.
# Build with `--target test` (or `docker compose run --rm test`).
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY . .
CMD ["pytest", "-q"]

FROM base AS runtime

COPY . .
RUN chmod +x docker-entrypoint.sh \
    && python manage.py collectstatic --noinput \
    && useradd --system --uid 1000 --home-dir /app gsr \
    && mkdir -p /data/storage \
    && chown -R gsr:gsr /data

# SQLite database and uploaded files live on the /data volume. Fontconfig caches the report's web fonts
# (WeasyPrint) under XDG_CACHE_HOME; the app user cannot write to its home directory.
ENV SQLITE_PATH=/data/db.sqlite3 \
    STORAGE_ROOT=/data/storage \
    XDG_CACHE_HOME=/tmp/.cache \
    PORT=8000

USER gsr
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT} --workers ${GUNICORN_WORKERS:-2} --timeout ${GUNICORN_TIMEOUT:-300}"]
