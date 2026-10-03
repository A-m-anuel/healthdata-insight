"""Data-quality scores derived from a :class:`ValidationReport`.

Dimension definitions (each a percentage):

* Completeness: expected fields present / expected fields, over in-scope
  resources (expected fields are listed in ``validation.FIELD_SPECS``).
* Validity: records with no errors / all records, including entries that
  were lost during parsing because they were malformed.
* Consistency: in-scope resources with no consistency issues / in-scope
  resources. Consistency issues include unresolved references, future
  dates, impossible date order, and contradictory statuses.
* Uniqueness: resources not flagged as duplicates / all resources.

The overall score is the unweighted mean of the dimensions that can be
computed.
"""

from __future__ import annotations

from statistics import mean

from healthdata_insight.models import (
    Dimension,
    DimensionScore,
    IssueCode,
    IssueHighlight,
    QualityGrade,
    QualityReport,
    Severity,
    ValidationReport,
)

GRADE_THRESHOLDS: tuple[tuple[float, QualityGrade], ...] = (
    (95.0, QualityGrade.EXCELLENT),
    (85.0, QualityGrade.GOOD),
    (70.0, QualityGrade.FAIR),
)

# Informational codes that say nothing about the quality of the clinical data.
_NOT_HIGHLIGHTED = frozenset({IssueCode.UNSUPPORTED_RESOURCE_TYPE, IssueCode.FORMAT_MISMATCH})


def grade_for(percent: float | None) -> QualityGrade:
    if percent is None:
        return QualityGrade.NOT_RATED
    for threshold, grade in GRADE_THRESHOLDS:
        if percent >= threshold:
            return grade
    return QualityGrade.POOR


def _records_with(report: ValidationReport, dimension: Dimension) -> set[int]:
    return {i.record_key for i in report.issues if i.dimension is dimension and i.record_key is not None}


def assess_quality(report: ValidationReport, max_highlights: int = 3) -> QualityReport:
    expected = sum(f.expected for f in report.field_completeness)
    present = sum(f.present for f in report.field_completeness)
    duplicates = _records_with(report, Dimension.UNIQUENESS)
    inconsistent = _records_with(report, Dimension.CONSISTENCY)

    completeness = DimensionScore(
        Dimension.COMPLETENESS, present, expected, "Expected fields that are populated"
    )
    validity = DimensionScore(
        Dimension.VALIDITY, report.valid_resource_count, report.total_records, "Records without errors"
    )
    consistency = DimensionScore(
        Dimension.CONSISTENCY, report.in_scope_count - len(inconsistent), report.in_scope_count,
        "In-scope resources with no contradictions",
    )
    uniqueness = DimensionScore(
        Dimension.UNIQUENESS, report.resource_count - len(duplicates), report.resource_count,
        "Resources that are not duplicates",
    )

    scores = [d.percent for d in (completeness, validity, consistency, uniqueness) if d.percent is not None]
    overall = mean(scores) if report.in_scope_count and scores else None

    # Rank issue codes by how many distinct records they affect; errors win ties.
    affected: dict[IssueCode, set] = {}
    has_error: set[IssueCode] = set()
    for issue in report.issues:
        if issue.code not in _NOT_HIGHLIGHTED:
            record = issue.record_key if issue.record_key is not None else (issue.file_name, issue.line)
            affected.setdefault(issue.code, set()).add(record)
            if issue.severity is Severity.ERROR:
                has_error.add(issue.code)
    ranked = sorted(affected, key=lambda code: (-len(affected[code]), code not in has_error))[:max_highlights]

    return QualityReport(
        completeness=completeness,
        validity=validity,
        consistency=consistency,
        uniqueness=uniqueness,
        missing_field_count=expected - present,
        duplicate_count=len(duplicates),
        invalid_record_count=report.invalid_record_count,
        overall_percent=overall,
        grade=grade_for(overall),
        highlights=tuple(IssueHighlight(code, len(affected[code])) for code in ranked),
    )
