"""Reports section: every downloadable output in one place."""

from __future__ import annotations

import logging
from collections.abc import Callable

import streamlit as st

from healthdata_insight import reports
from healthdata_insight.pipeline import ProcessingResult
from healthdata_insight.sanitize import SanitizationConfig
from healthdata_insight.storage import DatasetStore
from healthdata_insight.ui.common import current_sanitization, log_event
from healthdata_insight.ui.privacy import generate_sanitized_copy

logger = logging.getLogger(__name__)


def _cached(name: str, build: Callable[[], bytes]) -> bytes | None:
    """Build each report once per dataset and sanitization state, not on every rerun.

    Returns None (after showing an error) if the report can't be built.
    """
    result, sanitized = st.session_state.result, current_sanitization()
    # The sanitization's creation time identifies it; an object id could be reused after garbage collection.
    key = (result.fingerprint, result.processed_at, sanitized.created_at if sanitized else None, name)
    cache = st.session_state.setdefault("report_cache", {})
    if key not in cache:
        if len(cache) > 32:  # stale entries from earlier datasets
            cache.clear()
        try:
            cache[key] = build()
        except Exception:  # noqa: BLE001 - details go to the server log only
            logger.exception("Could not build report %s", name)
            st.error("This report could not be built because of an unexpected error.", icon="🚫")
            return None
    return cache[key]


def _download(label: str, name: str, file_name: str, mime: str, build: Callable[[], bytes], help: str) -> None:
    data = _cached(name, build)
    if data is None:
        return
    st.download_button(
        label, data, file_name=file_name, mime=mime, help=help, width="stretch",
        on_click=log_event, args=("report.downloaded", {"report": name}), key=f"download_{name}",
    )


def render_downloads(result: ProcessingResult, store: DatasetStore) -> None:
    sanitized = current_sanitization()
    st.caption("Reports contain counts, scores, issue codes, and PHI field locations. They never contain field "
               "values or file names; files are identified by SHA-256.")

    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            st.markdown("**Sanitized dataset**")
            if sanitized:
                st.caption(f"{len(sanitized.resources):,} resources, NDJSON. Settings: "
                           + ", ".join(f"{k}={v}" for k, v in sanitized.config.describe().items()))
                _download("Download sanitized dataset", "sanitized_dataset", "sanitized_dataset.ndjson",
                          "application/x-ndjson", sanitized.to_ndjson, "Sanitized copy of the dataset (see the Privacy section).")
            else:
                st.caption("Not generated yet. Customize it in the Privacy section, or use the default settings.")
                if st.button("Generate with default settings", key="generate_default_sanitized", width="stretch"):
                    generate_sanitized_copy(result, store, SanitizationConfig())
                    st.rerun()
        with st.container(border=True):
            st.markdown("**Data-quality report**")
            st.caption("Quality dimensions, indicators, most frequent issues, and field completeness (JSON).")
            _download("Download data-quality report", "data_quality_report", "data_quality_report.json",
                      "application/json", lambda: reports.to_json(reports.quality_report(result)),
                      "Data-quality scores and indicators.")
        with st.container(border=True):
            st.markdown("**Validation report**")
            st.caption("Summary, errors by resource type and field, and every issue (JSON), or issues only (CSV).")
            cols = st.columns(2)
            with cols[0]:
                _download("JSON", "validation_report", "validation_report.json", "application/json",
                          lambda: reports.to_json(reports.validation_report(result)), "Full validation report.")
            with cols[1]:
                _download("Issues CSV", "validation_issues", "validation_issues.csv", "text/csv",
                          lambda: reports.validation_issues_csv(result), "One row per validation issue.")
    with right:
        with st.container(border=True):
            st.markdown("**Privacy report**")
            st.caption("PHI findings by category and location (no values), plus sanitization results (JSON).")
            _download("Download privacy report", "privacy_report", "privacy_report.json", "application/json",
                      lambda: reports.to_json(reports.privacy_report(result, sanitized)),
                      "Potential PHI findings and sanitization summary.")
        with st.container(border=True):
            st.markdown("**Analytical summary**")
            st.caption("Demographics, condition prevalence, observation statistics, trends, and R statistics, "
                       "from the pseudonymized analytical dataset (JSON).")
            _download("Download analytical summary", "analytical_summary", "analytical_summary.json",
                      "application/json", lambda: reports.to_json(reports.analytical_summary(result)),
                      "Public-health summary statistics.")
        with st.container(border=True):
            st.markdown("**Everything**")
            st.caption("All reports" + (" and the sanitized dataset" if sanitized else "") + " in one ZIP file.")
            _download("Download all (ZIP)", "report_bundle", "healthdata_insight_reports.zip", "application/zip",
                      lambda: reports.report_bundle(result, sanitized), "All reports in one archive.")
