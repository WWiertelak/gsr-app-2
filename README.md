# gsr-app-2

GSR_APP 2.0 - GSR sensor data upload, cleaning, joint synchronization and co-arousal analysis (Django)

The processing pipeline is extracted from GSRv2 with unchanged results; the
segment-based analysis was dropped. See [docs/research-gsrv2.md](docs/research-gsrv2.md)
for how the pipeline works and what was carried over. The frontend is not chosen yet.

## Layout

- `backend/gsr/pipeline/` - Django-free pipeline: `parser` (eSense CSV), `preprocessing`
  (synchronization, cleaning, scaling, merge, downsampling), `synchrony` (joint
  synchronization), `coarousal` (co-arousal), `config` (`PipelineConfig`).
- `backend/gsr/models.py`, `backend/gsr/services/` - projects, dataset upload, analysis service.
- `backend/gsr/tests/` - unit tests and old-vs-new equivalence tests (synthetic data only).

## Run with Docker

The app runs as one container: on start it applies migrations, then serves Django with
gunicorn on port 8000. The SQLite database (`/data/db.sqlite3`) and uploaded files
(`/data/storage`) live on the `gsr_data` volume, so they survive restarts and rebuilds.

```bash
cp .env.example .env            # then set SECRET_KEY (see the comment in the file)
docker compose up -d --build    # http://localhost:8000/admin/
docker compose logs -f app

# Create an admin user / run the CLI analysis inside the container:
docker compose exec app python manage.py createsuperuser
docker compose cp a.csv app:/data/a.csv && docker compose cp b.csv app:/data/b.csv
docker compose exec app python manage.py gsr_analyze A=/data/a.csv B=/data/b.csv --output /data/result.json

# Run the test suite in the test image (adds dev dependencies):
docker compose --profile test run --rm test

docker compose down             # keeps data; add -v to delete the gsr_data volume
```

Without compose: `docker build -t gsr-app-2 backend` and
`docker run --env-file .env -p 127.0.0.1:8000:8000 -v gsr_data:/data gsr-app-2`.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | none (required when `DEBUG=0`) | Django secret key |
| `DEBUG` | `1` in settings, `0` in `.env.example` | Django debug mode |
| `ALLOWED_HOSTS` | `localhost` (`*` when `DEBUG=1`) | Comma-separated host names; keep `localhost` for the container healthcheck |
| `SECURE_SSL_REDIRECT` | `1` when `DEBUG=0` | Redirect HTTP to HTTPS; set `0` for plain local HTTP |
| `SECURE_COOKIES` | `1` when `DEBUG=0` | Secure flag on session/CSRF cookies; set `0` for plain local HTTP |
| `SECURE_HSTS_SECONDS` | `31536000` when `DEBUG=0` | HSTS max-age; set `0` without TLS |
| `MAX_UPLOAD_MB` | `100` | Upload size limit |
| `MAX_DATASETS` | `20` | Datasets per analysis |
| `SQLITE_PATH` | `/data/db.sqlite3` in the image | SQLite database file |
| `STORAGE_ROOT` | `/data/storage` in the image | Uploaded file storage |
| `APP_PORT` | `8000` | Host port (compose only, bound to 127.0.0.1) |
| `GUNICORN_WORKERS` | `2` | gunicorn worker count |
| `GUNICORN_TIMEOUT` | `300` | gunicorn worker timeout in seconds |

## Run locally

```bash
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python manage.py migrate
pytest -q
ruff check config gsr

# Analyze recordings without a UI:
python manage.py gsr_analyze A=path/a.csv B=path/b.csv --config '{"scaling_enabled": true}' --output result.json
```
