"""Overview, Data quality, and FHIR validation sections."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from healthdata_insight.analysis import completeness_frame, issue_breakdown_frame, issues_frame
from healthdata_insight.charts import horizontal_bar
from healthdata_insight.models import IssueCode, PhiSeverity
from healthdata_insight.pipeline import ProcessingResult
from healthdata_insight.r_charts import RStatus
from healthdata_insight.reports import errors_by
from healthdata_insight.summary import files_frame, format_bytes, resource_counts_frame
from healthdata_insight.ui.common import (
    GRADE_STYLE,
    chart_with_table,
    current_sanitization,
    number,
    pct,
    section_title,
)

MAX_ISSUE_ROWS = 5_000
PERCENT_BAR = {"Share (%)": st.column_config.ProgressColumn("Share", min_value=0, max_value=100, format="%.1f%%")}


def render_overview(result: ProcessingResult) -> None:
    summary, quality = result.analysis.summary, result.analysis.quality
    validation, privacy = result.analysis.validation, result.analysis.privacy
    counts = summary.in_scope_counts

    cols = st.columns(5)
    cols[0].metric("Total patients", number(counts["Patient"]), border=True,
                   help=f"{summary.distinct_patients:,} distinct patient IDs")
    cols[1].metric("Total resources", number(summary.total_resources), border=True,
                   help=f"{summary.resource_type_count} resource types in {summary.file_count} file(s)")
    cols[2].metric("Conditions", number(counts["Condition"]), border=True)
    cols[3].metric("Observations", number(counts["Observation"]), border=True)
    # Time only: a full timestamp doesn't fit a tile, and the date is in the caption under the title.
    cols[4].metric("Processed (UTC)", result.processed_at.strftime("%H:%M"), border=True,
                   help=f"{result.processed_at:%Y-%m-%d %H:%M:%S} UTC; processing took {result.duration_ms:,} ms.")

    sanitized = current_sanitization()
    cols = st.columns(4)
    cols[0].metric(f"Data quality ({quality.grade.value})", pct(quality.overall_percent, 0),
                   help="Mean of completeness, validity, consistency, and uniqueness.")
    cols[1].metric("Invalid resources", number(validation.invalid_record_count),
                   help=f"{validation.error_count:,} errors and {validation.warning_count:,} warnings.")
    cols[2].metric("Potential PHI findings", number(len(privacy.findings)),
                   help=f"High-risk: {privacy.count(PhiSeverity.HIGH):,}. "
                        + ("A sanitized copy has been generated." if sanitized else "No sanitized copy yet."))
    r_status = result.r_output.status
    cols[3].metric("Analytical records", number(result.analytics.record_count),
                   help="Pseudonymized records used for public-health analytics. R analysis: "
                        + ("completed" if r_status is RStatus.OK else r_status.value) + ".")

    GRADE_STYLE[quality.grade](quality.summary)

    left, right = st.columns([3, 2], gap="large")
    with left:
        section_title("Resources by type")
        frame = resource_counts_frame(summary)
        chart_with_table(
            horizontal_bar(frame, "Resource type", "Count", value_title="Resources",
                           tooltip=["Resource type", "Count", _pct_tooltip("Share (%)")]),
            frame, key="overview_types", column_config=PERCENT_BAR,
        )
    with right:
        section_title("Input files", f"{summary.file_count} file(s), {format_bytes(summary.total_bytes)} in total")
        st.dataframe(files_frame(result.files), hide_index=True)


def _pct_tooltip(field: str) -> alt.Tooltip:
    return alt.Tooltip(f"{field}:Q", format=".1f")


def render_quality(result: ProcessingResult) -> None:
    quality, validation = result.analysis.quality, result.analysis.validation
    GRADE_STYLE[quality.grade](quality.summary)

    cols = st.columns(6)
    cols[0].metric("Overall quality", pct(quality.overall_percent), border=True)
    cols[1].metric("Completeness", pct(quality.completeness.percent), border=True,
                   help=f"{quality.completeness.description}: {quality.completeness.passed:,} "
                        f"of {quality.completeness.total:,}.")
    cols[2].metric("Validity", pct(quality.validity.percent), border=True,
                   help=f"{quality.validity.description}: {quality.validity.passed:,} of {quality.validity.total:,}.")
    cols[3].metric("Duplicates", number(quality.duplicate_count), border=True,
                   help="Repeated ids, identical content, or Patients sharing an identifier.")
    cols[4].metric("Missing values", number(quality.missing_field_count), border=True,
                   help="Expected fields that are empty or absent, across in-scope resources.")
    cols[5].metric("Validation errors", number(validation.error_count), border=True)

    left, right = st.columns(2, gap="large")
    with left:
        section_title("Quality indicators", "Share of records passing each quality dimension.")
        dims = pd.DataFrame([
            {"Dimension": d.dimension.value.capitalize(), "Score (%)": d.percent, "Passed": d.passed,
             "Total": d.total, "Definition": d.description}
            for d in quality.dimensions if d.percent is not None
        ])
        if dims.empty:
            st.caption("No in-scope resources to score.")
        else:
            chart_with_table(
                horizontal_bar(dims, "Dimension", "Score (%)", value_title="Score (%)", value_format=".0f",
                               domain_max=100, tooltip=["Dimension", _pct_tooltip("Score (%)"), "Passed", "Total"]),
                dims, key="quality_dims",
            )
    with right:
        section_title("Least complete fields", "Expected fields with the lowest fill rate.")
        completeness = completeness_frame(validation)
        if completeness.empty:
            st.caption("No in-scope resources.")
        elif completeness["Missing"].sum() == 0:
            st.success("Every expected field is populated in every in-scope resource.", icon="✅")
        else:
            completeness["Resource field"] = completeness["Resource type"] + "." + completeness["Field"]
            lowest = completeness.sort_values("Completeness (%)").head(8)
            chart_with_table(
                horizontal_bar(lowest, "Resource field", "Completeness (%)", value_title="Completeness (%)",
                               value_format=".0f", domain_max=100, largest_first=False,
                               tooltip=["Resource field", _pct_tooltip("Completeness (%)"), "Present", "Missing"]),
                completeness.drop(columns=["Resource field"]), key="quality_fields",
                column_config={"Completeness (%)": st.column_config.ProgressColumn(
                    "Completeness", min_value=0, max_value=100, format="%.1f%%")},
            )

    if quality.highlights:
        section_title("Most frequent issues")
        st.dataframe(
            pd.DataFrame([{"Issue": h.code.description, "Code": h.code.value, "Records affected": h.records}
                          for h in quality.highlights]),
            hide_index=True,
        )


def render_validation(result: ProcessingResult, rejected_files: int) -> None:
    validation = result.analysis.validation
    cols = st.columns(5)
    cols[0].metric("Valid resources", number(validation.valid_resource_count), border=True)
    cols[1].metric("Invalid resources", number(validation.invalid_record_count), border=True,
                   help=f"Includes {validation.malformed_record_count:,} malformed entries that could not be read.")
    cols[2].metric("Errors", number(validation.error_count), border=True)
    cols[3].metric("Warnings", number(validation.warning_count), border=True)
    cols[4].metric("Rejected files", number(rejected_files), border=True,
                   help="Files that could not be read at all are not part of the results.")

    if not validation.issues:
        st.success("No validation issues found.", icon="✅")
        return

    by_type, by_field = errors_by(validation.issues, "resource_type"), errors_by(validation.issues, "path")
    left, right = st.columns(2, gap="large")
    with left:
        section_title("Errors by resource type")
        if by_type.empty:
            st.caption("No errors.")
        else:
            chart_with_table(horizontal_bar(by_type, "Resource type", "Errors", value_title="Errors"),
                             by_type, key="errors_type")
    with right:
        section_title("Errors by field", "Top 12 fields.")
        if by_field.empty:
            st.caption("No errors.")
        else:
            chart_with_table(horizontal_bar(by_field.head(12), "Field", "Errors", value_title="Errors"),
                             by_field, key="errors_field")

    section_title("Issues")
    breakdown, detail = st.tabs(["By issue type", "All issues"])
    with breakdown:
        st.dataframe(issue_breakdown_frame(validation.issues), hide_index=True)
    with detail:
        frame = issues_frame(validation.issues)
        frame["Resource type"] = frame["Resource"].str.split("/").str[0].str.replace(" (no id)", "", regex=False)
        cols = st.columns(3)
        severity = cols[0].multiselect("Severity", sorted(frame["Severity"].unique()), placeholder="All",
                                       key="val_severity")
        types = cols[1].multiselect("Resource type", sorted(t for t in frame["Resource type"].unique() if t),
                                    placeholder="All", key="val_type")
        codes = cols[2].multiselect("Issue code", sorted(c.value for c in IssueCode if c.value in set(frame["Code"])),
                                    placeholder="All", key="val_code")
        if severity:
            frame = frame[frame["Severity"].isin(severity)]
        if types:
            frame = frame[frame["Resource type"].isin(types)]
        if codes:
            frame = frame[frame["Code"].isin(codes)]
        st.caption(f"{len(frame):,} issues" + (f"; showing the first {MAX_ISSUE_ROWS:,}" if len(frame) > MAX_ISSUE_ROWS
                                              else ""))
        st.dataframe(frame.drop(columns=["Resource type"]).head(MAX_ISSUE_ROWS), hide_index=True,
                     key="val_issues_table")
