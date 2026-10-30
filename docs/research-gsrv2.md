# GSRv2 research: pipeline end to end, and what GSR_APP 2.0 carries over

Source studied: the local GSRv2 checkout at `/Users/wojciechwiertelak/GSRv2`, commit
`e16e2f6` ("new reports", 2026-07-24) **plus its uncommitted working-tree changes**.
The pipeline extracted here is that on-disk state, not just the last commit. The
frozen reference copy used by the equivalence tests is
`backend/gsr/tests/legacy_gsrv2/` (sha256 of each file is pinned in
`test_equivalence.py`).

## 1. GSRv2 at a glance

| Layer | GSRv2 | Files |
| --- | --- | --- |
| Backend | Django 5.2 + DRF, drf-spectacular OpenAPI, Celery + Redis jobs, Postgres | `backend/` |
| Frontend | React + TypeScript (Vite), ECharts | `frontend/` |
| Pipeline | Pure Python package (no Django imports): pandas, numpy, scipy, scikit-learn, statsmodels, ruptures, kneed, dtaidistance | `backend/gsr/pipeline/{parser,config,core}.py` |
| Reports | ReportLab PDF with bilingual (PL/EN) methodology text | `backend/gsr/pipeline/{reporting,methodology}.py` |
| Orchestration | Stage artifacts cached by config/file signature, async analysis/export jobs | `backend/gsr/services/analysis.py`, `tasks.py`, `views.py` |

The analysis flow in the UI is a sequence of stages:
`preview` (import + cleaning + scaling) → `segment` → `correlations` → `trends` →
`synchrony` → `contagion`, then optional CSV/PDF exports. All stages start from one
`run_pipeline()` call on the project's uploaded files.

## 2. Upload and parsing (`parser.py`)

Input is the CSV export of the **eSense Skin Response** sensor (~5 Hz), one file per participant.

1. **Upload** (`views.py` → `services/storage.py`): files are stored under
   `STORAGE_ROOT/projects/<project_id>/uploads/<timestamp>_<uuid>_<sanitized name>`,
   sha256 and size recorded, and each file is parsed once at upload time so broken
   files are rejected immediately (the stored file is deleted on failure). Limits:
   `MAX_UPLOAD_MB` (100) per request, `MAX_DATASETS` (20) per project, unique alias per
   project (NFC-normalized, ≤120 chars, no control characters).
2. **Header detection**: the first line (of the first 80) containing `MICROSIEMENS`
   and `;` is the table header; the metadata preamble above it is skipped.
3. **Encoding fallback**: `utf-8`, `utf-8-sig`, `cp1250`, `cp1252`, `latin-1`.
4. **Columns**: time marker `ZNACZNIKI CZASU` (Polish) or `TIMESTAMP` (English);
   `MICROSIEMENS`; optional `SEKUNDA`/`SECOND`. Comma decimals and spaces are normalized.
5. **Excel repair** *(uncommitted change)*: Excel rewrites decimals as dates when a CSV
   is opened and saved (`3.79` → `mar.79`, `1.02` → `1.lut`). Month tokens (Polish and
   English, all byte spellings of `paź`) are mapped back to numbers; the decimal count
   is inferred from the column's plain values. The repair count is logged and stored as
   `excel_repaired_values` metadata.
6. **Pause rows**: markers `Wstrzymane` / `Paused` *(English label is an uncommitted
   addition)* become NaN and are forward-filled.
7. **Timestamps**: `%H:%M:%S:%f`, falling back to `%H:%M:%S`; rows without
   milliseconds are rebuilt from the `SECOND` column relative to the first row that
   has both. Time-of-day is turned into a monotonic timeline on a 1900-01-01 base:
   a backwards jump of more than 12 h is a midnight rollover (+1 day), any other
   backwards step is a `ParseError`.
8. Rows without a valid time or microsiemens value are dropped. Output columns:
   `second, marker, time, microsiemens` plus metadata (skiprows, encoding, time
   column, row count, first/last marker).

## 3. Synchronization and cleaning (`core.run_pipeline`)

Per analysis run, for the selected datasets (in upload order):

1. **Duplicate check** *(uncommitted)*: recordings whose parsed microsiemens arrays
   are byte-identical are reported in `identical_signal_groups` (warning only, not excluded).
2. **Absolute-time synchronization**: the common window is
   `[max(start_i), min(end_i)]`. *(Uncommitted change: GSRv2's last commit fell back to
   per-file relative alignment when recordings did not overlap; the on-disk version
   refuses with "Datasets do not share an absolute-time overlap…".)* Each recording is
   cropped to the window and gets `relative_time` in minutes from its first kept sample.
   The window is reported as `alignment`.
3. **Cleaning** (per recording, on the synchronized samples):
   - artifact masks: non-finite values; physiological range
     (`0.5–100 µS` by default; the original GSRv2 upper limit was 20 µS);
     global z-score (`> 5 SD`); jump (`|Δ| > mean|Δ| + 5 SD`); IQR
     (`1.5×IQR`, **off** by default). Each mask is switchable; thresholds configurable.
   - rejected samples → NaN → linear interpolation (both directions), median fallback;
   - 4th-order Butterworth low-pass at 0.05 Hz (`filtfilt`, assumes fs = 5 Hz);
   - median filter (kernel 5, forced odd).
   - *(Uncommitted change: defaults moved to this conservative profile — previously
     median kernel 0, IQR on, z-score and jump thresholds 3.5.)*
   - signal quality per recording *(uncommitted)*: rejected share `< 10 %` good,
     `< 30 %` warning, otherwise poor (informational only).
4. **Time range**: optional `time_range_from/to` (relative minutes) crop after cleaning.
5. **Scaling**: optional per-recording min-max to `[0, 1]` (`scaling_enabled`).
6. **Merge**: recordings are joined on `relative_time` with `merge_asof(nearest)`,
   tolerance `0.51 × max(median sample step)`; rows without a partner are dropped
   (`merged_scaled`, the first recording's marker is kept).
7. **Downsampling** (`merged_decimated`):
   - scaling off → no downsampling (factor 1);
   - only `downsample_factor` explicitly set → legacy every-Nth-sample mode;
   - otherwise → mean per `downsample_interval_seconds` bucket (default 1 s).
   Fewer than 2 points is an error.
8. **Preview payload** (`serialize_preview`): raw/clean/scaled point series, common
   range, alignment, per-recording and total cleaning counts, signal quality, and
   *(uncommitted)* full-data RAW/CLEAN descriptive statistics.

In the app, every analysis stage required `scaling_enabled` ("Enable scaling before
running segmentation or segment-based analysis"), so synchrony and co-arousal always
ran on min-max scaled signals downsampled to 1 s buckets unless configured otherwise.

## 4. Segment-based analysis (dropped)

Built on `merged_decimated`:

- **Segmentation**: per recording, a change-intensity curve (left/right window mean
  and std difference) is smoothed; `ruptures.Window(rbf)` is fitted for k = 1…20
  breakpoints, k chosen by elbow (`kneed`) with BIC reported, overridable per
  recording. Breakpoints of all recordings are clustered within
  `cluster_tolerance_seconds`, filtered by `segment_width`, and annotated with a
  consensus curve and peaks (`strong_shared` / `moderate_shared` / `weak_or_individual`).
- **Correlations**: Spearman matrices globally and per segment, with p-values,
  FDR/Bonferroni/Holm correction, Fisher CIs and autocorrelation-adjusted effective N.
- **Trends**: per-segment linear slopes with `linregress` statistics.
- **Segment statistics / exports**: describe() per segment and recording, CSV export,
  persisted `Segment` rows.

## 5. Joint synchronization (`core.compute_synchrony`, carried over)

On z-scored `merged_decimated` signals, fs = 1 / median step:

- **Pairs**: max cross-correlation within ±`synchrony_max_lag_seconds` (10 s), its lag,
  zero-lag correlation, mean magnitude-squared coherence ≤ 1 Hz, normalized DTW
  distance (`dtaidistance`, optional). Signed by default: only in-phase coupling counts.
- **Surrogate p-values**: per pair and for the group mean, against nulls from
  autocorrelation-preserving surrogates (circular shift or phase randomization,
  200 by default, seeded → deterministic).
- **Rolling windows** (30 s window, 5 s step, Pearson or Spearman): per-pair series
  and group mean; per window the maximal cliques (Bron–Kerbosch) of pairs above the
  threshold (0.6) give active subgroups; consecutive windows with the same subgroup
  (gaps ≤ 15 s bridged, ≥ 30 s long, ≥ 2 members) are **synchrony episodes**.
- **Clusters**: average-linkage agglomerative clustering on `1 − synchrony`
  (distance threshold 0.5).

## 6. Co-arousal (`core.compute_contagion`, carried over as `compute_coarousal`)

Port of a standalone "emotional contagion" script, corrected in GSRv2:

- per-recording z-score within the analysis window; state High (`z > 1`), Low
  (`z < −1`) or neutral; state percentages per participant;
- sliding windows (30 s, step 5 s): a High/Low episode window needs ≥
  `contagion_min_participants` (2) in the same state for ≥ 60 % of samples; a
  Co-movement window needs mean pairwise Spearman ≥ 0.3; windows closer than 30 s merge;
- episode contributors: ≥ 50 % of samples in the state (High/Low) or leave-one-out
  Spearman ≥ 0.3 with the rest of the group (Co-movement); strength full/strong/partial;
- primary non-overlapping timeline (High > Low > Co-movement);
- **co-arousal index** = mean over samples of `max(#High, #Low)`, with a p-value from
  200 circular-shift surrogates (seeded);
- 1-minute heatmap of mean state per participant.

## 7. What GSR_APP 2.0 carries over

| GSRv2 | GSR_APP 2.0 | Change |
| --- | --- | --- |
| `pipeline/parser.py` | `backend/gsr/pipeline/parser.py` | same parsing and messages; `ParseError` also carries a `code` (and `row`) for the UI |
| `pipeline/config.py` | `backend/gsr/pipeline/config.py` | segment/correlation/UI fields removed; physiological upper default raised to 100 µS; explicit saved settings preserved |
| `core.run_pipeline`, cleaning, merge, downsampling, `serialize_preview`, `serialize_signal_statistics`, `serialize_decimated_traces`, `build_stage_export_frame` | `backend/gsr/pipeline/preprocessing.py` | segmentation removed from `run_pipeline` and `PipelineResult` |
| `core.compute_synchrony` and helpers | `backend/gsr/pipeline/synchrony.py` | unchanged code |
| `core.compute_contagion` and helpers | `backend/gsr/pipeline/coarousal.py` | renamed `compute_coarousal`; payload keys unchanged |
| shared helpers (`_zscore`, `_make_surrogate`, `_estimate_step_seconds`, `_resolve_aliases`, `PipelineError`) | `backend/gsr/pipeline/common.py` | moved; `PipelineError` messages unchanged, plus `code`/`alias`/`details` for the UI (`backend/gsr/errors.py`) |
| `Project` / `Dataset` models, upload storage + parse-on-upload | `backend/gsr/models.py`, `services/datasets.py` | minimal: no owner, notes, UI language; `Dataset.file` is a `FileField` under `STORAGE_ROOT` with the same path layout |
| analysis guards in `views.py` (scaling required, ≥ 2 datasets, min size/participants ≤ dataset count) | `services/analysis.py` (`analyze_files`, `run_project_analysis`) | same rules, plain Python service |
| `PipelineConfigSerializer` field types, bounds, choices and cross-field checks | `PipelineConfig.__init__` (`backend/gsr/pipeline/config.py`) | same rules and DRF-style coercion, enforced at construction; invalid values raise `ValueError` (`AnalysisError` from `resolve_config`, `CommandError` from `gsr_analyze`) |
| — | `manage.py gsr_analyze A=a.csv B=b.csv --config '{…}'` | new CLI entry point, alongside the UI |

Equivalence is enforced by `backend/gsr/tests/test_equivalence.py`: seven synthetic
scenarios (legacy defaults with an explicit 20 µS upper limit, unscaled full rate, legacy factor downsampling, strict
cleaning with time range, unsigned/phase/Spearman with 4 participants, surrogates
disabled, midnight crossing with cp1250 and Excel-mangled values) are run through the
frozen GSRv2 code and the new code, and parsing output, every cleaning/sync frame,
preview, traces, stage exports, synchrony and co-arousal payloads must be identical
(same keys, types and bit-identical floats). Pipeline error messages are compared too.
The GSRv2 unit tests for the parser, cleaning, synchrony and co-arousal are ported
in `test_parser.py` and `test_pipeline.py`.

All sample data is synthetic (`backend/gsr/tests/sample_data.py`); GSRv2's local
`output/` and presentation material contain participant names and were not used.

## 8. What was dropped

- Segment-based analysis: segmentation, consensus curve, breakpoint clustering,
  per-segment correlations, trends, segment statistics/exports, `Segment` model, and
  the `segment_width`, `segment_k_override(s)`, `cluster_tolerance_seconds` config.
- The global Spearman correlation matrix and its significance machinery
  (`correlation_*` config, statsmodels) — not segment-based, but outside the
  "joint synchronization and co-arousal" scope.
- PDF reports and methodology text (`reporting.py`, `methodology.py`, including the
  uncommitted methodology notes), presentations, `output/`. (The UI later brought the
  report back for synchronization and co-arousal: `backend/gsr/reporting/`.)
- API/UI layer: DRF endpoints and OpenAPI, React frontend, auth and first-run
  bootstrap (incl. the uncommitted `PASSWORDLESS_LOGIN` setting), throttling,
  Celery jobs with progress/cancellation, stage-artifact caching by signature,
  legacy-import and owner-check commands, the GSRv2 Docker/compose setup (GSR_APP 2.0
  has its own container setup, see the README).
- `signal_colors` and `scaling_method` config fields (UI-only / unused: min-max is the
  only scaling).

## 9. Behavior that is not preserved exactly

- **Error wording**: fewer than 2 points after downsampling raises
  "Not enough points for analysis" instead of "Not enough points for segmentation"
  (the only message change; asserted in the equivalence tests).
- **Config construction**: `PipelineConfig(**kwargs)` rejects the dropped fields with
  `TypeError`; `PipelineConfig.from_dict()` (used for stored project configs) still
  ignores them, so GSRv2 project configs load unchanged.
- **Entry-point name**: `compute_contagion` → `compute_coarousal`; config keys keep the
  `contagion_*` prefix for compatibility.
- **Numerical identity is per environment**: results are bit-identical when both
  pipelines run on the same library versions; dependencies are pinned to GSRv2's
  versions (pandas 2.3.2, numpy 2.3.2, scipy 1.16.1, scikit-learn 1.7.1,
  dtaidistance 2.3.13). Upgrading them may move floats in the last digits for both.
- **Run time**: segmentation is no longer computed on every pipeline run, so analyses
  are faster; outputs are unaffected (segmentation never altered the shared frames).
- **No caching or async jobs yet**: every call recomputes; GSRv2's cancellation hooks
  (`cancel_callback`) are kept in the analysis functions for a future job runner.
