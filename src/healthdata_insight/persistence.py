"""Persistent SQLite store for processing runs, results, and audit events.

Data minimisation: the persistent database never stores resource content,
field values, validation messages (they can quote values), or file names
(they can contain patient names). Files are identified by SHA-256 hash.
Resource ids are stored only as ``resource_ref``, a keyed hash whose key is
random per run and discarded, so refs link rows *within* a run but cannot
be traced back to the source data or linked across runs.

The uploaded data itself lives only in the per-session temporary store
(see :mod:`healthdata_insight.storage`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from healthdata_insight import __version__
from healthdata_insight.models import PrivacyReport, QualityReport, ValidationReport
from healthdata_insight.sanitize import SanitizationResult
from healthdata_insight.storage import FileRecord
from healthdata_insight.summary import DatasetSummary

DB_PATH_ENV = "HDI_DB_PATH"
DEFAULT_DB_PATH = Path("data") / "healthdata_insight.db"
SCHEMA_VERSION = 2

# Columns added after version 1; created on new databases and added in place to older ones.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("processing_runs", "input_records", "INTEGER"),
    ("processing_runs", "duration_ms", "INTEGER"),
    ("processing_runs", "analytical_records", "INTEGER"),
    ("processing_runs", "r_status", "TEXT"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS processing_runs (
    id             INTEGER PRIMARY KEY,
    run_uuid       TEXT    NOT NULL UNIQUE,
    created_at     TEXT    NOT NULL,
    app_version    TEXT    NOT NULL,
    file_count     INTEGER NOT NULL,
    total_bytes    INTEGER NOT NULL,
    resource_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS run_files (
    id                INTEGER PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
    sha256            TEXT    NOT NULL,
    size_bytes        INTEGER NOT NULL,
    format            TEXT    NOT NULL,
    resource_count    INTEGER NOT NULL,
    parse_issue_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_statistics (
    run_id         INTEGER NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
    resource_type  TEXT    NOT NULL,
    resource_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, resource_type)
);
CREATE TABLE IF NOT EXISTS validation_results (
    run_id             INTEGER PRIMARY KEY REFERENCES processing_runs(id) ON DELETE CASCADE,
    total_records      INTEGER NOT NULL,
    valid_resources    INTEGER NOT NULL,
    invalid_records    INTEGER NOT NULL,
    malformed_records  INTEGER NOT NULL,
    error_count        INTEGER NOT NULL,
    warning_count      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_issues (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
    code          TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    dimension     TEXT    NOT NULL,
    resource_type TEXT,
    resource_ref  TEXT,
    path          TEXT,
    line          INTEGER
);
CREATE TABLE IF NOT EXISTS quality_results (
    run_id              INTEGER PRIMARY KEY REFERENCES processing_runs(id) ON DELETE CASCADE,
    overall_percent     REAL,
    grade               TEXT    NOT NULL,
    completeness_pct    REAL,
    validity_pct        REAL,
    consistency_pct     REAL,
    uniqueness_pct      REAL,
    missing_field_count INTEGER NOT NULL,
    duplicate_count     INTEGER NOT NULL,
    invalid_record_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS phi_findings (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
    resource_type TEXT    NOT NULL,
    resource_ref  TEXT,
    path          TEXT    NOT NULL,
    category      TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    method        TEXT    NOT NULL,
    rule          TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS sanitization_results (
    id                     INTEGER PRIMARY KEY,
    run_id                 INTEGER NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
    created_at             TEXT    NOT NULL,
    config_json            TEXT    NOT NULL,
    actions_json           TEXT    NOT NULL,
    resources_out          INTEGER NOT NULL,
    residual_findings      INTEGER NOT NULL,
    residual_high_risk     INTEGER NOT NULL,
    errors_before          INTEGER NOT NULL,
    errors_after           INTEGER NOT NULL,
    references_preserved   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER REFERENCES processing_runs(id) ON DELETE SET NULL,
    occurred_at  TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    details_json TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_run ON validation_issues(run_id);
CREATE INDEX IF NOT EXISTS idx_phi_run ON phi_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);
"""


def default_db_path() -> Path:
    return Path(os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RunSummary:
    id: int
    run_uuid: str
    created_at: str
    file_count: int
    total_bytes: int
    input_records: int | None
    resource_count: int
    valid_resources: int
    invalid_records: int
    analytical_records: int | None
    duration_ms: int | None
    r_status: str | None
    overall_percent: float | None
    grade: str
    phi_findings: int
    sanitized_resources: int | None  # output of the latest sanitization, if any

    @property
    def sanitized(self) -> bool:
        return self.sanitized_resources is not None


class RunRepository:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            for table, column, kind in _ADDED_COLUMNS:
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            conn.execute("DELETE FROM schema_info")
            conn.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,))

    @property
    def schema_version(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT version FROM schema_info").fetchone()[0]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn

    # -- writes ----------------------------------------------------------------

    def record_run(
        self,
        files: Sequence[FileRecord],
        summary: DatasetSummary,
        validation: ValidationReport,
        quality: QualityReport,
        privacy: PrivacyReport,
        *,
        duration_ms: int | None = None,
        analytical_records: int | None = None,
        r_status: str | None = None,
    ) -> int:
        """Persist one analysis run atomically. Returns the run id."""
        ref_key = secrets.token_bytes(32)  # per-run; never stored

        def ref(resource_type: str | None, resource_id: str | None) -> str | None:
            if resource_type is None or resource_id is None:
                return None
            message = f"{resource_type}/{resource_id}".encode("utf-8")
            return hmac.new(ref_key, message, hashlib.sha256).hexdigest()[:16]

        with self._connect() as conn:
            run_id = conn.execute(
                """INSERT INTO processing_runs
                   (run_uuid, created_at, app_version, file_count, total_bytes, resource_count,
                    input_records, duration_ms, analytical_records, r_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), _now(), __version__, summary.file_count, summary.total_bytes,
                 summary.total_resources, validation.total_records, duration_ms, analytical_records, r_status),
            ).lastrowid
            conn.executemany(
                """INSERT INTO run_files (run_id, sha256, size_bytes, format, resource_count, parse_issue_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(run_id, f.sha256, f.size_bytes, f.format, f.resource_count, f.parse_issue_count) for f in files],
            )
            conn.executemany(
                "INSERT INTO resource_statistics (run_id, resource_type, resource_count) VALUES (?, ?, ?)",
                [(run_id, t, n) for t, n in summary.resource_counts.items()],
            )
            conn.execute(
                "INSERT INTO validation_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, validation.total_records, validation.valid_resource_count,
                 validation.invalid_record_count, validation.malformed_record_count,
                 validation.error_count, validation.warning_count),
            )
            conn.executemany(
                """INSERT INTO validation_issues
                   (run_id, code, severity, dimension, resource_type, resource_ref, path, line)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(run_id, i.code.value, i.severity.value, i.dimension.value, i.resource_type,
                  ref(i.resource_type, i.resource_id), i.path, i.line) for i in validation.issues],
            )
            conn.execute(
                "INSERT INTO quality_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, quality.overall_percent, quality.grade.value,
                 *(d.percent for d in quality.dimensions),
                 quality.missing_field_count, quality.duplicate_count, quality.invalid_record_count),
            )
            conn.executemany(
                """INSERT INTO phi_findings
                   (run_id, resource_type, resource_ref, path, category, severity, method, rule)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(run_id, f.resource_type, ref(f.resource_type, f.resource_id), f.path, f.category.value,
                  f.severity.value, f.method, f.rule) for f in privacy.findings],
            )
            self._log(conn, "run.completed", {
                "files": [f.sha256 for f in files],
                "resources": summary.total_resources,
                "errors": validation.error_count,
                "warnings": validation.warning_count,
                "phi_findings": len(privacy.findings),
            }, run_id)
        return run_id

    def record_sanitization(self, run_id: int, result: SanitizationResult) -> int:
        """Persist a summary of a sanitization (settings and counts; never the data)."""
        config = result.config.describe()
        with self._connect() as conn:
            row_id = conn.execute(
                """INSERT INTO sanitization_results
                   (run_id, created_at, config_json, actions_json, resources_out, residual_findings,
                    residual_high_risk, errors_before, errors_after, references_preserved)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, _now(), json.dumps(config), json.dumps(result.actions), len(result.resources),
                 len(result.residual.findings), result.residual_high_risk,
                 result.errors_before, result.errors_after,
                 int(result.references_preserved)),
            ).lastrowid
            self._log(conn, "sanitization.generated", {
                "config": config,
                "resources": len(result.resources),
                "residual_findings": len(result.residual.findings),
                "references_preserved": result.references_preserved,
            }, run_id)
        return row_id

    def log_event(self, event_type: str, details: dict[str, Any] | None = None, run_id: int | None = None) -> None:
        """Record an audit event. ``details`` must not contain PHI (no values, names, or file names)."""
        with self._connect() as conn:
            self._log(conn, event_type, details or {}, run_id)

    @staticmethod
    def _log(conn: sqlite3.Connection, event_type: str, details: dict[str, Any], run_id: int | None) -> None:
        conn.execute(
            "INSERT INTO audit_events (run_id, occurred_at, event_type, details_json) VALUES (?, ?, ?, ?)",
            (run_id, _now(), event_type, json.dumps(details, sort_keys=True)),
        )

    # -- reads -----------------------------------------------------------------

    def recent_runs(self, limit: int = 20) -> list[RunSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.id, r.run_uuid, r.created_at, r.file_count, r.total_bytes, r.input_records,
                          r.resource_count, v.valid_resources, v.invalid_records, r.analytical_records,
                          r.duration_ms, r.r_status, q.overall_percent, q.grade,
                          (SELECT COUNT(*) FROM phi_findings p WHERE p.run_id = r.id),
                          (SELECT s.resources_out FROM sanitization_results s WHERE s.run_id = r.id
                           ORDER BY s.id DESC LIMIT 1)
                   FROM processing_runs r
                   JOIN validation_results v ON v.run_id = r.id
                   JOIN quality_results q ON q.run_id = r.id
                   ORDER BY r.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [RunSummary(*row) for row in rows]

    def audit_events(self, run_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT id, run_id, occurred_at, event_type, details_json FROM audit_events"
        params: tuple = ()
        if run_id is not None:
            query += " WHERE run_id = ?"
            params = (run_id,)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY id", params).fetchall()
        return [
            {"id": i, "run_id": r, "occurred_at": t, "event_type": e, "details": json.loads(d)}
            for i, r, t, e, d in rows
        ]

    def table_counts(self, run_id: int) -> dict[str, int]:
        tables = ("run_files", "resource_statistics", "validation_results", "validation_issues",
                  "quality_results", "phi_findings", "sanitization_results", "audit_events")
        with self._connect() as conn:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE run_id = ?", (run_id,)).fetchone()[0]
                    for t in tables}
