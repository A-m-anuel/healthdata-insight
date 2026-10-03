"""Temporary SQLite storage for an uploaded dataset.

Each Streamlit session gets its own database in a private temporary
directory. Connections are opened per operation so the store can be used
safely across Streamlit's script-run threads and deleted cleanly on Windows.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import weakref
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from healthdata_insight.models import IssueCode, Severity, ValidationIssue
from healthdata_insight.parser import ParsedFile

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (
    id             INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL,
    sha256         TEXT    NOT NULL UNIQUE,
    size_bytes     INTEGER NOT NULL,
    format         TEXT    NOT NULL,
    resource_count INTEGER NOT NULL,
    bundle_count   INTEGER NOT NULL,
    uploaded_at    TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    resource_type TEXT    NOT NULL,
    resource_id   TEXT,
    payload       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resources_type ON resources(resource_type);
CREATE INDEX IF NOT EXISTS idx_resources_file ON resources(file_id);
CREATE TABLE IF NOT EXISTS parse_issues (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    severity TEXT    NOT NULL,
    code     TEXT    NOT NULL,
    line     INTEGER,
    message  TEXT    NOT NULL
);
"""


@dataclass(frozen=True)
class FileRecord:
    id: int
    name: str
    sha256: str
    size_bytes: int
    format: str
    resource_count: int
    bundle_count: int
    parse_issue_count: int
    uploaded_at: str


@dataclass(frozen=True)
class StoredResource:
    key: int  # resources.id
    file_name: str
    resource: dict[str, Any]


class DatasetStore:
    def __init__(self, db_path: Path, owned_dir: Path | None = None):
        self.db_path = Path(db_path)
        self._owned_dir = owned_dir
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @classmethod
    def create_temporary(cls) -> DatasetStore:
        directory = Path(tempfile.mkdtemp(prefix="healthdata_insight_"))
        store = cls(directory / "dataset.db", owned_dir=directory)
        # Remove the directory when the store is garbage-collected (e.g. the
        # Streamlit session ends) or, at the latest, when the process exits.
        weakref.finalize(store, shutil.rmtree, directory, ignore_errors=True)
        return store

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:  # commit on success, roll back on error
                yield conn

    def has_file(self, sha256: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM source_files WHERE sha256 = ?", (sha256,)).fetchone()
        return row is not None

    def add_file(self, parsed: ParsedFile) -> int:
        """Store a parsed file and its resources. Returns the file id."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO source_files
                   (name, sha256, size_bytes, format, resource_count, bundle_count, uploaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    parsed.name,
                    parsed.sha256,
                    parsed.size_bytes,
                    parsed.format.value,
                    len(parsed.resources),
                    parsed.bundle_count,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            file_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO resources (file_id, resource_type, resource_id, payload) VALUES (?, ?, ?, ?)",
                (
                    (file_id, r["resourceType"], _str_or_none(r.get("id")), json.dumps(r, separators=(",", ":")))
                    for r in parsed.resources
                ),
            )
            conn.executemany(
                "INSERT INTO parse_issues (file_id, severity, code, line, message) VALUES (?, ?, ?, ?, ?)",
                ((file_id, i.severity.value, i.code.value, i.line, i.message) for i in parsed.issues),
            )
        return file_id

    def retain_only(self, sha256s: Iterable[str]) -> int:
        """Remove every file (and its resources) not in ``sha256s``. Returns rows removed."""
        keep = set(sha256s)
        with self._connect() as conn:
            # Decided in Python rather than with "NOT IN (?, ?, ...)": SQLite limits the number of bound
            # parameters per statement (999 before 3.32), and an upload can contain more files than that.
            stale = [(file_id,) for file_id, sha256 in conn.execute("SELECT id, sha256 FROM source_files")
                     if sha256 not in keep]
            conn.executemany("DELETE FROM source_files WHERE id = ?", stale)
        return len(stale)

    def files(self) -> list[FileRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT f.id, f.name, f.sha256, f.size_bytes, f.format, f.resource_count,
                          f.bundle_count, COUNT(w.id), f.uploaded_at
                   FROM source_files f LEFT JOIN parse_issues w ON w.file_id = f.id
                   GROUP BY f.id ORDER BY f.id"""
            ).fetchall()
        return [FileRecord(*row) for row in rows]

    def resource_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT resource_type, COUNT(*) FROM resources GROUP BY resource_type"
            ).fetchall()
        return dict(rows)

    def distinct_resource_ids(self, resource_type: str) -> int:
        with self._connect() as conn:
            (count,) = conn.execute(
                "SELECT COUNT(DISTINCT resource_id) FROM resources WHERE resource_type = ?",
                (resource_type,),
            ).fetchone()
        return count

    def parse_issues(self) -> list[ValidationIssue]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT i.code, i.severity, i.message, f.name, i.line
                   FROM parse_issues i JOIN source_files f ON f.id = i.file_id
                   ORDER BY f.id, i.id"""
            ).fetchall()
        return [
            ValidationIssue(IssueCode(code), Severity(severity), message, file_name=name, line=line)
            for code, severity, message, name, line in rows
        ]

    def resources(self) -> list[StoredResource]:
        # Parse row by row instead of fetchall(): each JSON string is freed as soon as it is parsed,
        # so the raw text and the parsed dataset are never in memory at the same time.
        with self._connect() as conn:
            cursor = conn.execute(
                """SELECT r.id, f.name, r.payload
                   FROM resources r JOIN source_files f ON f.id = r.file_id
                   ORDER BY r.id"""
            )
            return [StoredResource(key, name, json.loads(payload)) for key, name, payload in cursor]

    def cleanup(self) -> None:
        """Delete the database (and its temp directory, if this store created it)."""
        if self._owned_dir is not None:
            shutil.rmtree(self._owned_dir, ignore_errors=True)
        else:
            self.db_path.unlink(missing_ok=True)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
