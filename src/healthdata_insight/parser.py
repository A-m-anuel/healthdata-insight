"""Reading FHIR R4 JSON / NDJSON files into a flat list of resources.

The parser is deliberately lenient about *content* (it does not validate
resources against the FHIR specification) but strict about *readability*:
a file must be UTF-8 text containing JSON or NDJSON with at least one
object carrying a ``resourceType``. Problems with individual entries (a bad
NDJSON line, an object without ``resourceType``, ...) are recorded as
issues and the rest of the file is still read. Resource content is checked
later, in :mod:`healthdata_insight.validation`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePath
from typing import Any

from healthdata_insight.config import (
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_MB,
    NDJSON_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
)
from healthdata_insight.models import IssueCode, Severity, ValidationIssue


TOO_DEEP = "JSON is nested too deeply to process."


class FileFormat(str, Enum):
    JSON = "JSON"
    NDJSON = "NDJSON"


@dataclass
class ParsedFile:
    name: str
    size_bytes: int
    sha256: str
    format: FileFormat
    resources: list[dict[str, Any]] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    bundle_count: int = 0

    def add_issue(self, code: IssueCode, severity: Severity, message: str, line: int | None = None) -> None:
        self.issues.append(ValidationIssue(code, severity, message, file_name=self.name, line=line))


class FHIRFileError(ValueError):
    """Raised when a file cannot be read as FHIR JSON/NDJSON at all."""

    def __init__(self, message: str, issues: list[ValidationIssue] | None = None):
        super().__init__(message)
        self.issues = issues or []


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_fhir_file(name: str, data: bytes) -> ParsedFile:
    """Parse an uploaded file into FHIR resources.

    Raises:
        FHIRFileError: if the file is unreadable or contains no FHIR resources.
    """
    extension = PurePath(name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        allowed = ", ".join(SUPPORTED_EXTENSIONS)
        raise FHIRFileError(f"Unsupported file type '{extension or name}'. Expected one of: {allowed}.")
    if len(data) > MAX_FILE_SIZE_BYTES:
        raise FHIRFileError(f"File exceeds the {MAX_FILE_SIZE_MB} MB size limit.")

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FHIRFileError(f"File is not valid UTF-8 text (byte {exc.start}).") from exc
    if not text.strip():
        raise FHIRFileError("File is empty.")

    parsed = ParsedFile(
        name=name,
        size_bytes=len(data),
        sha256=file_sha256(data),
        format=FileFormat.NDJSON if extension in NDJSON_EXTENSIONS else FileFormat.JSON,
    )

    if parsed.format is FileFormat.JSON:
        try:
            document = json.loads(text)
        except RecursionError as exc:
            raise FHIRFileError(TOO_DEEP) from exc
        except json.JSONDecodeError as exc:
            # "Extra data" after a complete first value usually means NDJSON
            # saved with a .json extension.
            if exc.msg != "Extra data":
                raise FHIRFileError(
                    f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}."
                ) from exc
            parsed.format = FileFormat.NDJSON
            parsed.add_issue(
                IssueCode.FORMAT_MISMATCH,
                Severity.WARNING,
                "File has a .json extension but contains NDJSON; read it as NDJSON.",
            )
        else:
            try:
                _collect(document, "document", parsed)
            except RecursionError as exc:
                raise FHIRFileError(TOO_DEEP) from exc

    if parsed.format is FileFormat.NDJSON:
        _parse_ndjson(text, parsed)

    if not parsed.resources:
        raise FHIRFileError("No FHIR resources were found in this file.", parsed.issues)
    return parsed


def _parse_ndjson(text: str, parsed: ParsedFile) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        resources_before, issues_before = len(parsed.resources), len(parsed.issues)
        try:
            _collect(json.loads(line), "top level", parsed, line_number)
        except json.JSONDecodeError as exc:
            parsed.add_issue(
                IssueCode.MALFORMED_JSON, Severity.ERROR, f"Invalid JSON ({exc.msg}); line skipped.", line_number
            )
        except RecursionError:
            # Drop anything collected from this line before the limit was hit.
            del parsed.resources[resources_before:], parsed.issues[issues_before:]
            parsed.add_issue(IssueCode.MALFORMED_JSON, Severity.ERROR, f"{TOO_DEEP} Line skipped.", line_number)


def _collect(value: Any, location: str, parsed: ParsedFile, line: int | None = None) -> None:
    """Append every FHIR resource found in ``value`` to ``parsed.resources``.

    Bundles are unpacked (recursively) rather than counted as resources.
    """
    if isinstance(value, list):
        for index, item in enumerate(value):
            _collect(item, f"{location}[{index}]", parsed, line)
        return

    if not isinstance(value, dict):
        parsed.add_issue(
            IssueCode.INVALID_STRUCTURE,
            Severity.ERROR,
            f"Expected a JSON object at {location}, found {type(value).__name__}; skipped.",
            line,
        )
        return

    resource_type = value.get("resourceType")
    if not isinstance(resource_type, str) or not resource_type:
        parsed.add_issue(
            IssueCode.MISSING_RESOURCE_TYPE, Severity.ERROR, f"Object at {location} has no 'resourceType'; skipped.", line
        )
        return

    if resource_type != "Bundle":
        parsed.resources.append(value)
        return

    parsed.bundle_count += 1
    entries = value.get("entry", [])
    if not isinstance(entries, list):
        parsed.add_issue(
            IssueCode.INVALID_STRUCTURE, Severity.ERROR, f"Bundle at {location} has a non-list 'entry'; skipped.", line
        )
        return
    if not entries:
        parsed.add_issue(IssueCode.EMPTY_BUNDLE, Severity.WARNING, f"Bundle at {location} contains no entries.", line)
    for index, entry in enumerate(entries):
        entry_location = f"{location}.entry[{index}]"
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if resource is None:
            # Legitimate in some transaction Bundles (e.g. DELETE entries), so only a warning.
            parsed.add_issue(
                IssueCode.INVALID_BUNDLE_ENTRY, Severity.WARNING, f"{entry_location} has no 'resource'; skipped.", line
            )
            continue
        _collect(resource, f"{entry_location}.resource", parsed, line)
