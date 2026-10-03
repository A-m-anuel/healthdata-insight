"""Glue between raw uploaded bytes and the dataset store."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from healthdata_insight.models import ValidationIssue
from healthdata_insight.parser import FHIRFileError, file_sha256, parse_fhir_file
from healthdata_insight.storage import DatasetStore


class IngestStatus(str, Enum):
    ADDED = "added"
    ALREADY_LOADED = "already_loaded"
    FAILED = "failed"


@dataclass(frozen=True)
class IngestResult:
    name: str
    sha256: str
    status: IngestStatus
    message: str = ""
    resource_count: int = 0
    issues: tuple[ValidationIssue, ...] = ()


def ingest_file(store: DatasetStore, name: str, data: bytes) -> IngestResult:
    """Parse ``data`` and add it to ``store`` unless identical content is already there."""
    sha256 = file_sha256(data)
    if store.has_file(sha256):
        return IngestResult(name, sha256, IngestStatus.ALREADY_LOADED)
    try:
        parsed = parse_fhir_file(name, data)
    except FHIRFileError as exc:
        return IngestResult(name, sha256, IngestStatus.FAILED, str(exc), issues=tuple(exc.issues))
    store.add_file(parsed)
    return IngestResult(
        name,
        sha256,
        IngestStatus.ADDED,
        resource_count=len(parsed.resources),
        issues=tuple(parsed.issues),
    )
