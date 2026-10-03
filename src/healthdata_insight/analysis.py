"""Runs summary, validation, quality, and PHI analysis over a stored dataset."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from healthdata_insight.models import (
    PhiFinding,
    PhiSeverity,
    PrivacyReport,
    QualityReport,
    Severity,
    ValidationIssue,
    ValidationReport,
)
from healthdata_insight.phi import detect_phi
from healthdata_insight.quality import assess_quality
from healthdata_insight.storage import DatasetStore, StoredResource
from healthdata_insight.summary import DatasetSummary, summarize_store
from healthdata_insight.validation import validate_dataset

ISSUE_COLUMNS = ["Severity", "Dimension", "Code", "Resource", "Field", "Message", "File", "Line"]


@dataclass(frozen=True)
class DatasetAnalysis:
    summary: DatasetSummary
    validation: ValidationReport
    quality: QualityReport
    privacy: PrivacyReport


def analyze_store(
    store: DatasetStore,
    *,
    resources: Sequence[StoredResource] | None = None,
    now: datetime | None = None,
) -> DatasetAnalysis:
    """Summary, validation, quality, and PHI scan. Pass ``resources`` if already loaded, to avoid a second copy."""
    resources = store.resources() if resources is None else resources
    validation = validate_dataset(resources, store.parse_issues(), now=now)
    return DatasetAnalysis(
        summarize_store(store), validation, assess_quality(validation), detect_phi(resources, now=now)
    )


def issues_frame(issues: Sequence[ValidationIssue]) -> pd.DataFrame:
    rows = [
        {
            "Severity": i.severity.value,
            "Dimension": i.dimension.value,
            "Code": i.code.value,
            "Resource": _resource_label(i),
            "Field": i.path or "",
            "Message": i.message,
            "File": i.file_name or "",
            "Line": i.line,
        }
        for i in issues
    ]
    return pd.DataFrame(rows, columns=ISSUE_COLUMNS).astype({"Line": "Int64"})


def issue_breakdown_frame(issues: Sequence[ValidationIssue]) -> pd.DataFrame:
    """One row per (severity, code), most frequent first."""
    columns = ["Severity", "Code", "Description", "Dimension", "Count"]
    if not issues:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(
        [{"Severity": i.severity.value, "Code": i.code.value, "Description": i.code.description,
          "Dimension": i.dimension.value} for i in issues]
    )
    grouped = frame.groupby(["Severity", "Code", "Description", "Dimension"]).size().reset_index(name="Count")
    severity_rank = grouped["Severity"].map({Severity.ERROR.value: 0, Severity.WARNING.value: 1})
    return grouped.assign(_rank=severity_rank).sort_values(["_rank", "Count"], ascending=[True, False]) \
        .drop(columns="_rank").reset_index(drop=True)[columns]


def completeness_frame(report: ValidationReport) -> pd.DataFrame:
    rows = [
        {
            "Resource type": f.resource_type,
            "Field": f.field,
            "Present": f.present,
            "Missing": f.missing,
            "Completeness (%)": f.percent,
        }
        for f in report.field_completeness
        if f.expected
    ]
    return pd.DataFrame(rows, columns=["Resource type", "Field", "Present", "Missing", "Completeness (%)"])


def _resource_label(issue: ValidationIssue) -> str:
    if issue.resource_type is None:
        return ""
    return f"{issue.resource_type}/{issue.resource_id}" if issue.resource_id else f"{issue.resource_type} (no id)"


_SEVERITY_ORDER = {PhiSeverity.HIGH: 0, PhiSeverity.MEDIUM: 1, PhiSeverity.LOW: 2}


def phi_category_frame(report: PrivacyReport) -> pd.DataFrame:
    """One row per PHI category: findings, resources affected, and highest severity."""
    columns = ["Category", "Findings", "Resources", "Highest severity"]
    groups: dict[str, list[PhiFinding]] = {}
    for finding in report.findings:
        groups.setdefault(finding.category.value, []).append(finding)
    rows = [
        {
            "Category": category,
            "Findings": len(findings),
            "Resources": len({f.record_key for f in findings}),
            "Highest severity": min((f.severity for f in findings), key=_SEVERITY_ORDER.__getitem__).value,
        }
        for category, findings in groups.items()
    ]
    return pd.DataFrame(rows, columns=columns).sort_values("Findings", ascending=False, ignore_index=True)


def phi_findings_frame(findings: Sequence[PhiFinding]) -> pd.DataFrame:
    rows = [
        {
            "Resource": f"{f.resource_type}/{f.resource_id}" if f.resource_id else f"{f.resource_type} (no id)",
            "Path": f.path,
            "Category": f.category.value,
            "Severity": f.severity.value,
            "Method": f.method,
            "Rule": f.rule,
        }
        for f in findings
    ]
    return pd.DataFrame(rows, columns=["Resource", "Path", "Category", "Severity", "Method", "Rule"])
