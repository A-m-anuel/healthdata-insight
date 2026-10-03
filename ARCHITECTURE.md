# Architecture

HealthData Insight is a single-user Streamlit application backed by a plain, Streamlit-independent Python
package (`src/healthdata_insight/`), an optional R script, and two SQLite databases. This document describes how
data flows through it and why it is built this way. It is a research and portfolio **prototype**: see
[LIMITATIONS.md](LIMITATIONS.md).

## Data flow

```
 Upload (JSON / NDJSON)                                     Streamlit session
        │
        ▼
 parser.py ──► ingest.py ──► storage.py  (temporary SQLite, one per session, raw resources)
                                  │
                     "Process dataset" (pipeline.py)
                                  │
        ┌─────────────────────────┼──────────────────────────────┬────────────────────────┐
        ▼                         ▼                              ▼                        ▼
 validation.py              phi.py                      analytics.py               summary.py
 (issues, completeness,     (PHI findings:              (strict sanitization  ──►  r_charts.py ──► R script
  duplicates)                locations only)              + allow-list extract)     (pseudonymized CSVs only)
        │                         │                              │
        ▼                         │                              │
 quality.py (scores)              │                              │
        └────────────┬────────────┴──────────────┬───────────────┘
                     ▼                           ▼
        persistence.py (history DB:      ui/ (dashboard sections)  ──►  reports.py (downloads)
        counts, codes, hashed refs)                                ──►  sanitize.py (optional sanitized copy)
```

1. **Parse** (`parser.py`). Files are decoded as UTF-8 and parsed as JSON or NDJSON. Bundles are unpacked
   recursively. Problems with individual entries become coded parse issues, and the rest of the file is still read.
   A whole file is rejected if it is unreadable, empty, too large (200 MB), nested too deeply, or contains no
   resources.
2. **Store** (`storage.py`). Each browser session has a private temporary SQLite database holding the raw resources
   as JSON. It is the only place where raw uploaded content is kept, and it is deleted when the session ends or
   the process exits.
3. **Process** (`pipeline.py`). This runs when the user clicks *Process dataset*. The resources are loaded **once**,
   then:
   - `validation.py` checks every resource against the rules for its type (structure, required and important
     fields, code sets, dates, references) and finds duplicates;
   - `quality.py` derives the four quality dimensions from the issues;
   - `phi.py` scans structured fields for potential PHI;
   - `analytics.py` builds the pseudonymized analytical dataset;
   - `r_charts.py` runs R on that dataset;
   - `persistence.py` records the run.
4. **Present** (`ui/`, `charts.py`). Each dashboard section is a module that reads the immutable `ProcessingResult`.
   The result is a snapshot: if the uploaded files change, the dashboard keeps it and asks the user to process
   again.
5. **Export** (`reports.py`, `sanitize.py`). Reports are built from the snapshot. The optional sanitized copy is
   produced on demand, then validated and re-scanned.

## Modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Scope (Patient, Condition, Observation), supported formats, size limit |
| `fhir.py` | FHIR R4 primitives: the 146 resource types, id rules, dates as ranges (partial dates have a precision), reference parsing |
| `models.py` | The shared result model: `ValidationIssue` (code, severity, dimension), `ValidationReport`, `QualityReport`, `PhiFinding`, `PrivacyReport` |
| `parser.py`, `ingest.py`, `storage.py` | Reading files into the temporary store |
| `validation.py` | Validation rules (`FIELD_SPECS` defines expected fields once, for both missing-field issues and completeness), duplicate detection, out-of-scope summary |
| `quality.py` | Completeness, validity, consistency, uniqueness; overall grade; most frequent issues |
| `phi.py` | Deterministic PHI rules on structured fields; findings never carry values |
| `sanitize.py` | Configurable sanitization: removal, generalization, keyed pseudonymization, reference rewriting, date coarsening |
| `analytics.py` | Pseudonymized analytical dataset and public-health statistics |
| `r_charts.py`, `r/analytics_plots.R` | Runs base-R statistics and charts in a subprocess |
| `pipeline.py` | The processing step, with no Streamlit dependency (used by the app, the tests, and the evaluation) |
| `persistence.py` | History database: runs, statistics, results, PHI findings, audit events |
| `reports.py` | Downloadable JSON/CSV reports and the ZIP bundle |
| `charts.py`, `ui/` | Altair charts and the dashboard sections |

`app.py` holds only session state, the sidebar workflow, and the page frame. Everything below the UI is plain
Python, which is why the whole workflow can be tested and evaluated without a browser.

## Design decisions

**One issue model.** Every problem, from a broken NDJSON line to a duplicate Patient, is a `ValidationIssue` with a
stable code. Each code belongs to exactly one quality dimension, so the quality scores, the dashboard tables, the
reports, and the history database all derive from the same list.

**Expected fields are declared once.** `FIELD_SPECS` drives both the missing-field issues and the completeness
score, so the "missing values" count and the completeness percentage can't disagree. The evaluation checks this
exactly (E3 in [EVALUATION.md](EVALUATION.md)).

**Partial dates are ranges.** "1980" means all of 1980. An event is reported as "before birth" only when it is
*certainly* earlier, which avoids false positives when dates have different precision.

**Out-of-scope resources are counted, not analyzed.** They are checked for a valid resource type and id, included
in duplicate detection and the PHI scan (narrative, Patient-reference display text), and summarized as **one**
warning per type rather than one per resource. Before this change, a Synthea export produced hundreds of thousands
of identical warnings.

**Analytics never see raw data.** `analytics.py` excludes invalid and duplicate records, runs the sanitizer with a
fixed strict configuration (pseudonymous keys from a per-run random key, birth year only with ages 90+ grouped,
state only, clinical dates by month), and then extracts only an allow-list of columns. Free text is never extracted.
Public-health charts, the Data Explorer, R, and the analytical summary all read this dataset.

**R is optional and isolated.** R runs as a subprocess (`Rscript --vanilla`, no shell) on CSV extracts in a private
temporary directory that is deleted afterwards. The script uses base R only. If R is missing, fails, or times out,
the result says so and the dashboard falls back to its Python charts.

**History without raw data.** The history database stores counts, scores, issue codes, PHI *paths*, and audit
events. It never stores resource content, field values, validation messages, file names (files are identified by
SHA-256), or pseudonymization secrets. Resource ids appear only as `resource_ref`, an HMAC with a per-run random
key that is discarded, so rows link within a run but can't be traced back to the source data.

## Storage

### Temporary dataset (per session)

| Table | Contents |
| --- | --- |
| `source_files` | File name, SHA-256 (unique), size, format, resource and bundle counts |
| `resources` | One row per resource: type, id, raw JSON (`payload`); indexed by type and file |
| `parse_issues` | Coded problems found while reading each file |

Foreign keys cascade, so removing a file from the uploader removes its resources and issues. Connections are
opened per operation, which suits Streamlit's threads and lets the file be deleted cleanly on Windows.

### History database (`data/healthdata_insight.db`, or `HDI_DB_PATH`)

| Table | Contents |
| --- | --- |
| `processing_runs` | UUID, timestamp, app version, file count, bytes, input records, resources, processing time, analytical records, R status |
| `run_files` | SHA-256, size, format, counts (no names) |
| `resource_statistics` | Resources per type per run |
| `validation_results`, `validation_issues` | Summary counts; issue code, severity, dimension, type, hashed `resource_ref`, path, line (no messages) |
| `quality_results` | Dimension scores, grade, missing fields, duplicates, invalid records |
| `phi_findings` | Type, hashed `resource_ref`, path, category, severity, method, rule |
| `sanitization_results` | Settings (never the secret), action counts, residual findings, errors before and after, references preserved |
| `audit_events` | `run.completed`, `file.rejected`, `sanitization.generated`, `report.downloaded`, `dataset.cleared`; details as JSON without PHI |
| `schema_info` | Schema version (2); older databases are upgraded in place |

Each run is written in one transaction. If the database can't be written, the app keeps working and shows a
warning.

## Technologies

| Purpose | Technology |
| --- | --- |
| Language | Python 3.11+ (developed and evaluated on 3.13) |
| UI | Streamlit; Altair (Vega-Lite) charts |
| Data handling | pandas, NumPy |
| Storage | SQLite (Python standard library) |
| Statistics and charts | R (base R only; tested with 4.6.1) |
| Tests | pytest, Streamlit `AppTest`, coverage |

## Scaling characteristics

The pipeline is **whole-dataset and in memory**. All resources are loaded into Python objects, because duplicate
detection and reference resolution need the whole dataset at once. Memory therefore grows linearly with input
size. The measured figures are in [EVALUATION.md](EVALUATION.md) (E5/E6), and the consequences are described in
[LIMITATIONS.md](LIMITATIONS.md).
