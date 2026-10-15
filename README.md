# gsr-app-2

GSR_APP 2.0 - GSR sensor data upload, cleaning, joint synchronization and co-arousal analysis (Django)

## Quick start

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker
   Engine with the Compose plugin) and start it.
2. Get the code: `git clone https://github.com/WWiertelak/gsr-app-2.git && cd gsr-app-2`
3. Start the app: `docker compose up -d --build` (the first build takes a few minutes).
   No configuration file is needed.
4. Open <http://localhost:8017/> in your browser.

To stop it, run `docker compose down`. Your projects and uploaded files are kept.

### Log in to the admin

The admin panel is at <http://localhost:8017/admin/>. Create an admin account once:

```bash
docker compose exec app python manage.py createsuperuser
```

Enter a username, an optional email and a password when asked, then log in at
<http://localhost:8017/admin/> with that username and password.

The processing pipeline is extracted from GSRv2 with unchanged results; the
segment-based analysis was dropped. See [docs/research-gsrv2.md](docs/research-gsrv2.md)
for how the pipeline works and what was carried over.

The UI is server-rendered Django templates with HTMX and ECharts (no Node build), in
Polish or English (switch in the left rail). The workflow is
Projects → 1 Import → 2 Cleaning → 3 Analysis (Synchronization | Co-arousal tabs, one run)
→ 4 Interviews (VSR interview moments per student pair and for the teacher, chosen from the
Analysis episodes by the steps of the "VSR Interview Flow Chart", each with a preview of the
signals and a one-sentence justification from the Analysis values; a lesson timeline shows every
candidate moment the Analysis found, one row per step, and the interview's signals across the whole
lesson; the researcher accepts, rejects, reorders, retimes or adds moments, also straight from the
timeline, and choosing again keeps those decisions) → 5 Export (bilingual PDF report
for other researchers, with the interview moments and their justifications as its own
section; CSV tables including `vsr_moments.csv`, a run settings + results JSON, chart PNGs, or
all of it as a ZIP). Re-running or changing an earlier step marks later saved results
outdated until each is refreshed in order. New projects start with the values the VSR Interview Flow
Chart suggests (co-movement correlation above 0.8 for the synchronization and co-movement thresholds,
5 moments per interview plus 3 in reserve, and step 5 "no synchrony" for a correlation below 0.1, set
on the Interviews step); projects saved earlier keep their settings (step 5 below 0) until "Reset
defaults" in the Analysis settings sets them. Interview pairs can be added and removed
with the form buttons; removed pairs retain curated decisions for restoration when
the same pair is added again. Moments show lesson time and the GSR sensor clock,
which the researcher uses to locate the moment. There is no login yet: add Django
authentication before hosting the app for more than one person.

## Layout

- `backend/gsr/pipeline/` - Django-free pipeline: `parser` (eSense CSV), `preprocessing`
  (synchronization, cleaning, scaling, merge, downsampling), `synchrony` (joint
  synchronization), `coarousal` (co-arousal), `vsr` (VSR interview moment selection),
  `config` (`PipelineConfig`).
- `backend/gsr/models.py`, `backend/gsr/services/` - projects, dataset upload, analysis service,
  stored last results per screen (`results`), exports (`exports`), interview roles and the
  number of moments kept outside the analysis signature (`interview`), the Interviews step's
  moments chosen from the stored analysis run (`vsr`), the researcher's decisions on each
  interview's list of moments (`curation`, model `InterviewMoment`).
- `backend/gsr/views.py`, `backend/gsr/templates/gsr/`, `backend/gsr/static/gsr/` - UI screens
  (`vsr_text.py` writes each moment's justification sentence in the UI language);
  `static/gsr/js/charts.js` holds the ECharts option builders ported from GSRv2,
  `static/gsr/vendor/` the pinned htmx and ECharts builds and the IBM Plex web fonts (`fonts/`, SIL OFL).
- `backend/gsr/reporting/` - PDF report (HTML template converted to PDF by WeasyPrint; shares the app's `static/gsr/css/tokens.css` and IBM Plex fonts, with bundled DejaVu fonts as the glyph fallback) and its methodology text; `interviews` lays out the VSR interview moments section and `vsr_charts`
  draws its SVG charts on the server.
- `backend/gsr/locale/pl/` - Polish UI strings. After changing UI text run
  `python manage.py makemessages -l pl --no-obsolete --ignore=.venv --ignore=staticfiles`, translate,
  then `python manage.py compilemessages --ignore=.venv` (needs gettext) and commit both files.
- `backend/gsr/tests/` - unit tests and old-vs-new equivalence tests (synthetic data only).

## Run with Docker

The app runs as one container: on start it applies migrations, then serves Django with
gunicorn on container port 8000, published on host port 8017. The SQLite database
(`/data/db.sqlite3`), uploaded files (`/data/storage`) and the generated secret key
(`/data/secret_key`) live on the `gsr_data` volume, so they survive restarts and rebuilds.

```bash
docker compose up -d --build    # UI at http://localhost:8017/ (admin at /admin/)
docker compose logs -f app

# Create an admin user / run the CLI analysis inside the container:
docker compose exec app python manage.py createsuperuser
docker compose cp a.csv app:/data/a.csv && docker compose cp b.csv app:/data/b.csv
docker compose exec app python manage.py gsr_analyze A=/data/a.csv B=/data/b.csv \
    --config '{"scaling_enabled": true}' --output /data/result.json

# Run the test suite in the test image (adds dev dependencies):
docker compose --profile test run --rm test

docker compose down             # keeps data; add -v to delete the gsr_data volume
```

Without compose: `docker build -t gsr-app-2 backend` and
`docker run --env-file .env.example -p 127.0.0.1:8017:8000 -v gsr_data:/data gsr-app-2`.

### Environment variables

None are required. `docker-compose.yml` loads safe local defaults (plain HTTP on
localhost) from `.env.example`. To change them, copy `.env.example` to `.env` and edit it;
values in `.env` override the defaults. Hosting the app beyond localhost needs at
least `ALLOWED_HOSTS` and, behind TLS, the `SECURE_*` settings.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | generated once into `secret_key` next to the SQLite file (`/data/secret_key` in the image) when `DEBUG=0` | Django secret key; set it only to use your own |
| `DEBUG` | `1` in settings, `0` in compose | Django debug mode |
| `ALLOWED_HOSTS` | `localhost` (`*` when `DEBUG=1`); `localhost,127.0.0.1` in compose | Comma-separated host names; keep `localhost` for the container healthcheck |
| `SECURE_SSL_REDIRECT` | `1` when `DEBUG=0`; `0` in compose | Redirect HTTP to HTTPS; set `1` behind a TLS proxy |
| `SECURE_COOKIES` | `1` when `DEBUG=0`; `0` in compose | Secure flag on session/CSRF cookies; set `1` with TLS |
| `SECURE_HSTS_SECONDS` | `31536000` when `DEBUG=0`; `0` in compose | HSTS max-age; keep `0` without TLS |
| `MAX_UPLOAD_MB` | `100` | Upload size limit |
| `MAX_DATASETS` | `20` | Datasets per analysis |
| `SQLITE_PATH` | `/data/db.sqlite3` in the image | SQLite database file |
| `STORAGE_ROOT` | `/data/storage` in the image | Uploaded file storage |
| `APP_PORT` | `8017` | Host port (compose only, bound to 127.0.0.1) |
| `GUNICORN_WORKERS` | `2` | gunicorn worker count |
| `GUNICORN_TIMEOUT` | `300` | gunicorn worker timeout in seconds |

## Run locally

```bash
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py runserver      # UI at http://127.0.0.1:8000/
pytest -q
ruff check config gsr

# Analyze recordings without a UI:
python manage.py gsr_analyze A=path/a.csv B=path/b.csv --config '{"scaling_enabled": true}' --output result.json
```
