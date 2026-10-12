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

## Run

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
