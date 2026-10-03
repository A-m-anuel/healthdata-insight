"""Downloadable reports built from a processing result.

Reports follow the same data-minimisation rules as the history database:
no field values, no resource content, no file names (files are identified by
SHA-256). Validation messages never quote Patient PHI values. Resource ids
are included where needed to locate a problem.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from datetime import date, datetime
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from healthdata_insight import __version__
from healthdata_insight.analysis import completeness_frame, issues_frame, phi_category_frame
from healthdata_insight.analytics import (
    age_distribution,
    condition_frequencies,
    gender_distribution,
    geography,
    observation_statistics,
    temporal_trend,
)
from healthdata_insight.config import APP_NAME
from healthdata_insight.models import PhiSeverity
from healthdata_insight.pipeline import ProcessingResult
from healthdata_insight.r_charts import RStatus
from healthdata_insight.sanitize import SanitizationResult

PRIVACY_DISCLAIMER = (
    "Potential PHI was detected with deterministic rules on structured FHIR fields; identifying details in "
    "free text or non-standard fields may be missed. This report supports a privacy review. It is not a "
    "determination of HIPAA compliance or of de-identification status."
)


# --------------------------------------------------------------------------- serialization

def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else round(value, 4)
    if value is pd.NA or value is pd.NaT:
        return None
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    clean = frame.astype(object).where(frame.notna(), None)
    return [{k: _jsonable(v) if not isinstance(v, (str, int, bool, type(None))) else v for k, v in row.items()}
            for row in clean.to_dict("records")]


def _clean(value: Any) -> Any:
    """Recursively make a report JSON-safe.

    ``json.dumps`` only calls ``default`` for types it can't encode itself; floats (including numpy's
    float64, a float subclass) bypass it, so NaN and infinity must be replaced before encoding.
    """
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else float(value)
    return value


def to_json(report: dict[str, Any]) -> bytes:
    # allow_nan=False: fail loudly rather than ever emit the invalid tokens NaN / Infinity.
    return json.dumps(_clean(report), indent=2, default=_jsonable, ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _header(result: ProcessingResult, report_type: str) -> dict[str, Any]:
    summary = result.analysis.summary
    return {
        "report": report_type,
        "generator": f"{APP_NAME} {__version__}",
        "generated_at": datetime.now(result.processed_at.tzinfo).isoformat(timespec="seconds"),
        "processed_at": result.processed_at.isoformat(timespec="seconds"),
        "run_id": result.run_id,
        "dataset": {
            "files": [{"sha256": sha} for sha in result.fingerprint],
            "total_bytes": summary.total_bytes,
            "resources": summary.total_resources,
        },
    }


# --------------------------------------------------------------------------- reports

def quality_report(result: ProcessingResult) -> dict[str, Any]:
    quality, validation = result.analysis.quality, result.analysis.validation
    return _header(result, "data-quality") | {
        "overall": {"percent": quality.overall_percent, "grade": quality.grade, "summary": quality.summary},
        "dimensions": [
            {"dimension": d.dimension, "description": d.description, "passed": d.passed, "total": d.total,
             "percent": d.percent}
            for d in quality.dimensions
        ],
        "indicators": {
            "missing_fields": quality.missing_field_count,
            "duplicate_records": quality.duplicate_count,
            "invalid_records": quality.invalid_record_count,
            "validation_errors": validation.error_count,
            "validation_warnings": validation.warning_count,
        },
        "most_frequent_issues": [{"code": h.code, "description": h.code.description, "records": h.records}
                                 for h in quality.highlights],
        "field_completeness": records(completeness_frame(validation)),
    }


def validation_report(result: ProcessingResult) -> dict[str, Any]:
    validation = result.analysis.validation
    issues = issues_frame(validation.issues).drop(columns=["File"])
    return _header(result, "fhir-validation") | {
        "summary": {
            "total_records": validation.total_records,
            "valid_resources": validation.valid_resource_count,
            "invalid_records": validation.invalid_record_count,
            "malformed_records": validation.malformed_record_count,
            "errors": validation.error_count,
            "warnings": validation.warning_count,
        },
        "errors_by_resource_type": records(errors_by(validation.issues, "resource_type")),
        "errors_by_field": records(errors_by(validation.issues, "path")),
        "issues": records(issues),
    }


def csv_bytes(frame: pd.DataFrame) -> bytes:
    """CSV for download, with spreadsheet formula injection neutralized.

    Text cells come from uploaded data (codes, labels, ids). A cell starting with = + - @ (or a tab or
    carriage return) is run as a formula by spreadsheet programs, so such cells get a leading apostrophe.
    """
    def neutralize(value):
        if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", chr(9), chr(13)):
            return "'" + value
        return value

    text_columns = [c for c in frame.columns if frame[c].dtype == object or pd.api.types.is_string_dtype(frame[c])]
    safe = frame.copy()
    for column in text_columns:
        safe[column] = safe[column].map(neutralize)
    return safe.to_csv(index=False).encode("utf-8")


def validation_issues_csv(result: ProcessingResult) -> bytes:
    return csv_bytes(issues_frame(result.analysis.validation.issues).drop(columns=["File"]))


def privacy_report(result: ProcessingResult, sanitization: SanitizationResult | None) -> dict[str, Any]:
    privacy = result.analysis.privacy
    report = _header(result, "privacy") | {
        "disclaimer": PRIVACY_DISCLAIMER,
        "summary": {
            "potential_phi_findings": len(privacy.findings),
            "resources_scanned": privacy.resources_scanned,
            "resources_affected": privacy.resources_affected,
            "patients_affected": privacy.patients_affected,
            "by_severity": {s.value: privacy.count(s) for s in PhiSeverity},
        },
        "categories": records(phi_category_frame(privacy)),
        "findings": [
            {"resource_type": f.resource_type, "resource_id": f.resource_id, "path": f.path,
             "category": f.category, "severity": f.severity, "method": f.method, "rule": f.rule}
            for f in privacy.findings
        ],
        "sanitization": {"status": "not generated"},
    }
    if sanitization is not None:
        report["sanitization"] = {
            "status": "generated",
            "generated_at": sanitization.created_at.isoformat(timespec="seconds"),
            "settings": sanitization.config.describe(),
            "changes": sanitization.actions,
            "resources_out": len(sanitization.resources),
            "remaining_findings": len(sanitization.residual.findings),
            "remaining_high_risk": sanitization.residual_high_risk,
            "errors_before": sanitization.errors_before,
            "errors_after": sanitization.errors_after,
            "new_error_types": sorted(sanitization.new_error_codes),
            "references_preserved": sanitization.references_preserved,
        }
    return report


def analytical_summary(result: ProcessingResult) -> dict[str, Any]:
    data = result.analytics
    r = result.r_output
    return _header(result, "analytical-summary") | {
        "basis": ("Pseudonymized analytical dataset: valid, de-duplicated resources; pseudonymous keys; "
                  "birth year only (ages 90+ grouped); state/country only; clinical dates by month."),
        "records": {
            "patients": len(data.patients),
            "conditions": len(data.conditions),
            "observations": len(data.observations),
            "excluded_invalid": data.excluded_invalid,
            "excluded_duplicates": data.excluded_duplicates,
        },
        "demographics": {
            "gender": records(gender_distribution(data)),
            "age_group_by_gender": records(age_distribution(data).astype({"age_group": str})),
            "geography": records(geography(data)),
        },
        "conditions": records(condition_frequencies(data)),
        "observations": records(observation_statistics(data)),
        "trends": {
            "condition_onsets": records(_period_labels(temporal_trend(data.conditions["onset_month"]))),
            "observations": records(_period_labels(temporal_trend(data.observations["effective_month"]))),
        },
        "r_analysis": {
            "status": r.status,
            "r_version": r.r_version,
            "message": r.message or None,
            "tables": {name: records(table) for name, table in r.tables.items()} if r.status is RStatus.OK else {},
        },
    }


def _period_labels(trend: pd.DataFrame) -> pd.DataFrame:
    """Label periods at their own precision ("2021" or "2021-03"), not as midnight timestamps."""
    if trend.empty:
        return trend
    fmt = "%Y" if trend["granularity"].iloc[0] == "year" else "%Y-%m"
    return trend.assign(period=trend["period"].dt.strftime(fmt))


def errors_by(issues, attribute: str) -> pd.DataFrame:
    """Error counts grouped by an issue attribute (``resource_type`` or ``path``)."""
    column = {"resource_type": "Resource type", "path": "Field"}[attribute]
    errors = [getattr(i, attribute) or "(unreadable entry)" for i in issues if i.severity.value == "error"]
    if not errors:
        return pd.DataFrame(columns=[column, "Errors"])
    counts = pd.Series(errors).value_counts()
    return counts.rename_axis(column).reset_index(name="Errors")


def report_bundle(result: ProcessingResult, sanitization: SanitizationResult | None) -> bytes:
    """All reports (and the sanitized dataset, if generated) in one ZIP."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("data_quality_report.json", to_json(quality_report(result)))
        bundle.writestr("validation_report.json", to_json(validation_report(result)))
        bundle.writestr("validation_issues.csv", validation_issues_csv(result))
        bundle.writestr("privacy_report.json", to_json(privacy_report(result, sanitization)))
        bundle.writestr("analytical_summary.json", to_json(analytical_summary(result)))
        if sanitization is not None:
            bundle.writestr("sanitized_dataset.ndjson", sanitization.to_ndjson())
            bundle.writestr("sanitization_settings.json", json.dumps(sanitization.config.describe(), indent=2))
    return buffer.getvalue()

