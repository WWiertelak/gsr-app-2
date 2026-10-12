# Frozen GSRv2 reference pipeline (test-only)

Verbatim copies of GSRv2 `backend/gsr/pipeline/config.py`, `parser.py` and `core.py`
as they were on disk at commit `e16e2f6` plus the uncommitted working-tree changes.
`gsr/tests/test_equivalence.py` runs this code and the new `gsr.pipeline` side by
side and requires identical outputs; it also pins each file's sha256, so do not edit
these files. They are excluded from linting and need the test-only dependencies in
`requirements-dev.txt` (ruptures, kneed, statsmodels).
