"""Result model shared by parsing, validation, and data-quality analysis.

Every problem found anywhere in the pipeline is a :class:`ValidationIssue`
with a stable :class:`IssueCode`. Each code belongs to exactly one quality
:class:`Dimension`, which is how issues roll up into the quality scores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"      # the record is invalid
    WARNING = "warning"  # the record is usable, but something is off


class Dimension(str, Enum):
    STRUCTURE = "structure"
    VALIDITY = "validity"
    COMPLETENESS = "completeness"
    CONSISTENCY = "consistency"
    UNIQUENESS = "uniqueness"


class IssueCode(str, Enum):
    # Structure: the data cannot be read as the FHIR resource it claims to be.
    MALFORMED_JSON = "malformed-json"
    MISSING_RESOURCE_TYPE = "missing-resource-type"
    INVALID_STRUCTURE = "invalid-structure"
    INVALID_BUNDLE_ENTRY = "invalid-bundle-entry"
    EMPTY_BUNDLE = "empty-bundle"
    FORMAT_MISMATCH = "format-mismatch"
    UNKNOWN_RESOURCE_TYPE = "unknown-resource-type"
    UNSUPPORTED_RESOURCE_TYPE = "unsupported-resource-type"
    # Validity: a value is present but not allowed.
    INVALID_ID = "invalid-id"
    INVALID_CODE = "invalid-code"
    INVALID_DATE = "invalid-date"
    INVALID_VALUE = "invalid-value"
    INVALID_REFERENCE = "invalid-reference"
    INCOMPLETE_CODING = "incomplete-coding"
    # Completeness: an expected value is absent.
    MISSING_ID = "missing-id"
    MISSING_REQUIRED_FIELD = "missing-required-field"
    MISSING_FIELD = "missing-field"
    MISSING_CODE = "missing-code"
    MISSING_VALUE = "missing-value"
    # Consistency: values contradict each other or the rest of the dataset.
    UNRESOLVED_REFERENCE = "unresolved-reference"
    FUTURE_DATE = "future-date"
    INCONSISTENT_DATES = "inconsistent-dates"
    DATE_BEFORE_BIRTH = "date-before-birth"
    STATUS_CONFLICT = "status-conflict"
    # Uniqueness: the record duplicates another one.
    DUPLICATE_RECORD = "duplicate-record"
    CONFLICTING_DUPLICATE_ID = "conflicting-duplicate-id"

    @property
    def dimension(self) -> Dimension:
        return _DIMENSIONS[self]

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]


_DIMENSIONS: dict[IssueCode, Dimension] = {}
_DESCRIPTIONS: dict[IssueCode, str] = {}


def _register(dimension: Dimension, entries: dict[IssueCode, str]) -> None:
    for code, description in entries.items():
        _DIMENSIONS[code] = dimension
        _DESCRIPTIONS[code] = description


_register(Dimension.STRUCTURE, {
    IssueCode.MALFORMED_JSON: "Line is not valid JSON",
    IssueCode.MISSING_RESOURCE_TYPE: "Object has no resourceType",
    IssueCode.INVALID_STRUCTURE: "Element has the wrong JSON type or shape",
    IssueCode.INVALID_BUNDLE_ENTRY: "Bundle entry without a resource",
    IssueCode.EMPTY_BUNDLE: "Bundle without entries",
    IssueCode.FORMAT_MISMATCH: "File extension does not match its content",
    IssueCode.UNKNOWN_RESOURCE_TYPE: "Not a FHIR R4 resource type",
    IssueCode.UNSUPPORTED_RESOURCE_TYPE: "Resource type outside the analysis scope",
})
_register(Dimension.VALIDITY, {
    IssueCode.INVALID_ID: "Resource id has an invalid format",
    IssueCode.INVALID_CODE: "Value is not in the allowed code set",
    IssueCode.INVALID_DATE: "Date is malformed or does not exist",
    IssueCode.INVALID_VALUE: "Value has the wrong type or is incomplete",
    IssueCode.INVALID_REFERENCE: "Reference is malformed or targets the wrong type",
    IssueCode.INCOMPLETE_CODING: "Coding lacks a system or code",
})
_register(Dimension.COMPLETENESS, {
    IssueCode.MISSING_ID: "Resource has no id",
    IssueCode.MISSING_REQUIRED_FIELD: "Field required by FHIR R4 is missing",
    IssueCode.MISSING_FIELD: "Important field is missing",
    IssueCode.MISSING_CODE: "Clinical code is missing",
    IssueCode.MISSING_VALUE: "Observation has no value",
})
_register(Dimension.CONSISTENCY, {
    IssueCode.UNRESOLVED_REFERENCE: "Referenced resource is not in the dataset",
    IssueCode.FUTURE_DATE: "Date is in the future",
    IssueCode.INCONSISTENT_DATES: "Dates are in an impossible order",
    IssueCode.DATE_BEFORE_BIRTH: "Event is dated before the patient's birth",
    IssueCode.STATUS_CONFLICT: "Status contradicts other fields",
})
_register(Dimension.UNIQUENESS, {
    IssueCode.DUPLICATE_RECORD: "Record duplicates another record",
    IssueCode.CONFLICTING_DUPLICATE_ID: "Id reused by a different record",
})
assert set(_DIMENSIONS) == set(IssueCode), "every IssueCode needs a dimension"


@dataclass(frozen=True)
class ValidationIssue:
    code: IssueCode
    severity: Severity
    message: str
    resource_type: str | None = None
    resource_id: str | None = None
    path: str | None = None
    file_name: str | None = None
    line: int | None = None
    record_key: int | None = None  # storage row of the resource; None for parse-level issues

    @property
    def dimension(self) -> Dimension:
        return self.code.dimension

    def __str__(self) -> str:
        return f"Line {self.line}: {self.message}" if self.line else self.message


@dataclass(frozen=True)
class FieldCompleteness:
    resource_type: str
    field: str
    expected: int
    present: int

    @property
    def missing(self) -> int:
        return self.expected - self.present

    @property
    def percent(self) -> float | None:
        return 100 * self.present / self.expected if self.expected else None


@dataclass(frozen=True)
class ValidationReport:
    resource_count: int
    in_scope_count: int
    malformed_record_count: int  # entries lost during parsing (parse-level errors)
    invalid_resource_keys: frozenset[int]
    issues: tuple[ValidationIssue, ...]
    field_completeness: tuple[FieldCompleteness, ...]

    @property
    def total_records(self) -> int:
        return self.resource_count + self.malformed_record_count

    @property
    def valid_resource_count(self) -> int:
        return self.resource_count - len(self.invalid_resource_keys)

    @property
    def invalid_record_count(self) -> int:
        return len(self.invalid_resource_keys) + self.malformed_record_count

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.WARNING)


@dataclass(frozen=True)
class DimensionScore:
    dimension: Dimension
    passed: int
    total: int
    description: str

    @property
    def percent(self) -> float | None:
        return 100 * self.passed / self.total if self.total else None


class QualityGrade(str, Enum):
    EXCELLENT = "Excellent"
    GOOD = "Good"
    FAIR = "Fair"
    POOR = "Poor"
    NOT_RATED = "Not rated"


@dataclass(frozen=True)
class IssueHighlight:
    code: IssueCode
    records: int


@dataclass(frozen=True)
class QualityReport:
    completeness: DimensionScore
    validity: DimensionScore
    consistency: DimensionScore
    uniqueness: DimensionScore
    missing_field_count: int
    duplicate_count: int
    invalid_record_count: int
    overall_percent: float | None
    grade: QualityGrade
    highlights: tuple[IssueHighlight, ...] = field(default_factory=tuple)

    @property
    def dimensions(self) -> tuple[DimensionScore, ...]:
        return (self.completeness, self.validity, self.consistency, self.uniqueness)

    @property
    def summary(self) -> str:
        if self.overall_percent is None:
            return "No in-scope data to assess."
        text = f"{self.grade.value} overall data quality ({self.overall_percent:.0f}%)."
        if not self.highlights:
            return text + " No issues found."
        top = "; ".join(f"{_lower_first(h.code.description)} ({h.records:,})" for h in self.highlights)
        return f"{text} Most frequent issues: {top}."


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


# --------------------------------------------------------------------------- privacy

class PhiCategory(str, Enum):
    NAME = "name"
    TELECOM = "telecom"
    ADDRESS = "address"
    IDENTIFIER = "identifier"
    DATE = "date"
    PHOTO = "photo"
    FREE_TEXT = "free-text"


class PhiSeverity(str, Enum):
    HIGH = "high"      # direct identifier (name, phone, record number, street address, ...)
    MEDIUM = "medium"  # quasi-identifier (full birth date, city, postal code, narrative text, ...)
    LOW = "low"        # coarse quasi-identifier (birth year, state, country)


DETECTION_METHOD = "fhir-field-rule"


@dataclass(frozen=True)
class PhiFinding:
    """A field that may hold PHI. Deliberately records *where*, never the value itself."""

    resource_type: str
    resource_id: str | None
    path: str  # e.g. "Patient.address[0].postalCode"
    category: PhiCategory
    severity: PhiSeverity
    rule: str  # which deterministic rule fired, e.g. "patient-address-postal-code"
    method: str = DETECTION_METHOD
    record_key: int | None = None


@dataclass(frozen=True)
class PrivacyReport:
    findings: tuple[PhiFinding, ...]
    resources_scanned: int

    @property
    def resources_affected(self) -> int:
        return len({f.record_key for f in self.findings})

    @property
    def patients_affected(self) -> int:
        return len({f.record_key for f in self.findings if f.resource_type == "Patient"})

    @property
    def category_counts(self) -> dict[PhiCategory, int]:
        counts: dict[PhiCategory, int] = {}
        for finding in self.findings:
            counts[finding.category] = counts.get(finding.category, 0) + 1
        return counts

    def count(self, severity: PhiSeverity) -> int:
        return sum(1 for f in self.findings if f.severity is severity)
