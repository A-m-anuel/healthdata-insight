# Security and privacy

HealthData Insight is a **prototype** built for synthetic data. It has not been through a security audit, a
penetration test, or a formal privacy review. It is not HIPAA compliant and must not be used with real patient
data without an organization's own review and authorization. This document describes what the prototype does to
protect data, and where it stops.

## Intended deployment

A single user runs the app locally (`streamlit run app.py`) on their own machine, with synthetic or authorized
data. There is **no authentication, authorization, or multi-user isolation**. Anyone who can reach the Streamlit
port can use the app and read the shared history database. Don't expose the app on a network.

## Data handled

| Data | Where it lives | Lifetime |
| --- | --- | --- |
| Uploaded files (raw resources, including any PHI) | Temporary SQLite database in a private temp directory, one per browser session | Until the session's store is garbage-collected, **Clear dataset** is clicked, or the process exits |
| Processing results (issues, findings, analytics) | Server memory (Streamlit session state) | Until the session ends |
| History: counts, scores, issue codes, PHI field paths, audit events | `data/healthdata_insight.db` (or `HDI_DB_PATH`) | Indefinitely (there is no retention policy) |
| R inputs: pseudonymized analytical CSVs | Private temp directory | Deleted as soon as R finishes |
| Downloads (reports, sanitized copy, explorer CSVs) | The user's browser | Under the user's control |

## Controls

### Input handling
- **Allowed file types:** only `.json`, `.ndjson`, and `.jsonl`, enforced by the uploader and the parser.
- **Size limit:** 200 MB per file, enforced both by Streamlit (`server.maxUploadSize`) and by the parser.
- **Encoding:** files must be strict UTF-8. Invalid bytes reject the file with the byte offset.
- **Deep nesting:** deeply nested JSON (a cheap denial-of-service input) is rejected with "nested too deeply", or,
  in NDJSON, that line is skipped. It no longer crashes the parser. *(Fixed during the Phase 6 review.)*
- **Bad entries:** malformed entries are recorded as coded issues, never executed or interpreted. There is no
  `eval`, no pickle, no dynamic import, and no shell.

### Output encoding
- **Markdown injection:** uploaded file names are escaped before they appear in Markdown messages, so a name like
  `![x](https://…)` can't load remote content or inject links. *(Fixed during the Phase 6 review.)*
- **CSV formula injection:** cells in the CSV downloads (validation issues, Data Explorer) that start with
  `=`, `+`, `-`, `@`, a tab, or a carriage return get a leading apostrophe. *(Fixed during the Phase 6 review.)*
- **Valid JSON:** JSON reports are written with `allow_nan=False` after NaN and infinity are converted to `null`,
  so they are always valid JSON. *(Fixed during the Phase 6 review.)*
- **HTML:** `unsafe_allow_html` is used once, for a fixed CSS rule that contains no user data.

### Database and subprocess
- **SQL injection:** all SQL is parameterized. Table and column names in SQL are constants, never user input.
- **R subprocess:** R runs as `Rscript --vanilla script in_dir out_dir` with an argument list (no shell), a
  120-second timeout, and only the pseudonymized analytical extract as input.

### Data minimization
- **History database:** it never stores resource content, field values, validation messages (they can quote
  values), file names (files are identified by SHA-256), or pseudonymization secrets. Resource ids are stored only
  as per-run keyed hashes. A test scans every text value in the database for source values.
- **Reports:** reports never contain field values or file names.
- **Validation messages:** messages never quote Patient birth dates, death dates, or identifier values.
- **Analytics and the Data Explorer:** these read only the pseudonymized, allow-listed analytical dataset.
  Tests check that no source name, identifier, contact detail, full date, postal code, city, or resource id appears
  in it.
- **Logs:** the app's own log messages contain no file names or field values. An unexpected file error is logged
  with the file's SHA-256 instead of its name. *(Fixed during the Phase 6 review.)*

### Pseudonymization
- **Keys:** pseudonyms are HMAC-SHA256 values. Without a user-supplied secret, the key is 32 random bytes per run
  and is never stored.
- **Secrets:** a user-supplied secret (for stable, linkable pseudonyms) is held only in session memory and never
  logged, stored, or included in reports or settings summaries.

## Known gaps

- **No access control.** Single-user and local only, as described above.
- **Data at rest is not encrypted.** The temporary dataset holds raw uploaded data in a temp directory. It isn't
  encrypted and is readable by the operating-system user.
- **Session data in memory.** Processing results, including validation messages that may contain non-Patient
  values and resource ids, stay in server memory for the session.
- **Tracebacks.** Unexpected errors are logged with tracebacks. A traceback could include a value from the record
  being processed when the error happened.
- **Rule-based PHI detection.** Detection only covers specific structured fields. It misses PHI in free text, in
  non-standard extensions, and in address geolocation. See [EVALUATION.md](EVALUATION.md) (E2).
- **Sanitization reduces risk but doesn't guarantee de-identification.**
  - It doesn't modify free text other than narratives. In particular, `valueString` and `note` are kept.
  - Quasi-identifiers remain in the output, such as provider addresses, which often share the patient's ZIP code
    (measured in E7).
  - Any combination of the kept attributes may still single out a person in a small dataset.
- **File names are shown in the session UI.** The uploader and the Overview's file table show uploaded file names,
  which can contain patient names (Synthea's do). They are never stored or exported.
- **Dependencies are not pinned in `pyproject.toml`.** `requirements-lock.txt` records the exact versions used for
  the evaluation. No dependency vulnerability scan has been run.

## Reporting a problem

This is a portfolio prototype with no security support. If you find a problem, please open an issue describing
it. Don't include real patient data.
