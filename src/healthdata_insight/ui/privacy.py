"""Privacy section: PHI findings and the optional sanitized copy."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import streamlit as st

from healthdata_insight.analysis import phi_category_frame, phi_findings_frame
from healthdata_insight.charts import horizontal_bar
from healthdata_insight.models import PhiSeverity
from healthdata_insight.pipeline import ProcessingResult, dataset_fingerprint
from healthdata_insight.reports import PRIVACY_DISCLAIMER
from healthdata_insight.sanitize import (
    AddressAction,
    EventDateAction,
    IdentifierAction,
    PersonDateAction,
    SanitizationConfig,
    SanitizationResult,
    sanitize_dataset,
)
from healthdata_insight.storage import DatasetStore
from healthdata_insight.ui.common import (
    chart_with_table,
    current_sanitization,
    log_event,
    number,
    persist,
    repo,
    section_title,
)

ACTION_LABELS: dict[Any, str] = {
    IdentifierAction.PSEUDONYMIZE: "Replace with pseudonyms",
    IdentifierAction.REMOVE: "Remove",
    AddressAction.GENERALIZE: "Keep state and country only",
    AddressAction.REMOVE: "Remove",
    PersonDateAction.YEAR: "Keep year only",
    PersonDateAction.REMOVE: "Remove",
    PersonDateAction.KEEP: "Keep unchanged",
    EventDateAction.KEEP: "Keep unchanged",
    EventDateAction.MONTH: "Keep year and month",
    EventDateAction.YEAR: "Keep year only",
}
MAX_ROWS = 5_000
logger = logging.getLogger(__name__)


def render_privacy(result: ProcessingResult, store: DatasetStore) -> None:
    privacy = result.analysis.privacy
    st.caption(PRIVACY_DISCLAIMER)

    cols = st.columns(4)
    cols[0].metric("Potential PHI findings", number(len(privacy.findings)), border=True,
                   help=" · ".join(f"{s.value}: {privacy.count(s):,}" for s in PhiSeverity))
    cols[1].metric("PHI categories", len(privacy.category_counts), border=True,
                   help=", ".join(c.value for c in privacy.category_counts) or "None")
    cols[2].metric("Resources affected", number(privacy.resources_affected), border=True,
                   help=f"{privacy.patients_affected:,} Patients; {privacy.resources_scanned:,} resources scanned.")
    status = cols[3].empty()  # filled at the end, after a sanitization may have been generated below

    if privacy.findings:
        left, right = st.columns([2, 3], gap="large")
        with left:
            section_title("Findings by PHI category")
            categories = phi_category_frame(privacy)
            chart_with_table(horizontal_bar(categories, "Category", "Findings", value_title="Findings",
                                            tooltip=["Category", "Findings", "Resources", "Highest severity"]),
                             categories, key="phi_categories")
        with right:
            section_title("Findings", "Locations of potential PHI. Values are never displayed or stored.")
            findings = phi_findings_frame(privacy.findings)
            cols = st.columns(2)
            severity = cols[0].multiselect("Severity", [s.value for s in PhiSeverity], placeholder="All",
                                           key="phi_severity")
            category = cols[1].multiselect("Category", sorted(findings["Category"].unique()), placeholder="All",
                                           key="phi_category")
            if severity:
                findings = findings[findings["Severity"].isin(severity)]
            if category:
                findings = findings[findings["Category"].isin(category)]
            st.dataframe(findings.head(MAX_ROWS), hide_index=True, height=300, key="phi_findings_table")
    else:
        st.success("No potential PHI found in the scanned fields.", icon="✅")

    st.divider()
    section_title("Sanitized copy", "Optional. Produces an NDJSON copy with identifiers removed or pseudonymized, then validates and re-scans it. Sanitization reduces risk; it does not guarantee de-identification.")
    if st.toggle("Generate a sanitized copy of this dataset", key="sanitize_enabled"):
        render_sanitization_form(result, store)
    sanitized = current_sanitization()
    if sanitized:
        render_sanitization_result(sanitized)
    status.metric("Sanitization", "Generated" if sanitized else "Not yet", border=True,
                  help="Whether a sanitized copy has been generated for the current dataset.")


def render_sanitization_form(result: ProcessingResult, store: DatasetStore) -> None:
    defaults = SanitizationConfig()

    def choice(column, label: str, options: type, default, **kwargs):
        """A selectbox that starts on the configuration's default, not on the first option."""
        members = list(options)
        return column.selectbox(label, members, index=members.index(default), format_func=ACTION_LABELS.get,
                                **kwargs)

    with st.form("sanitization_form"):
        left, right = st.columns(2)
        identifiers = choice(left, "Patient identifiers", IdentifierAction, defaults.identifiers)
        addresses = choice(left, "Patient addresses", AddressAction, defaults.addresses)
        group_ages = left.checkbox("Group ages 90 and over", value=defaults.group_ages_over_89,
                                   help="With 'Keep year only', birth years 90+ years ago are set to "
                                        "the current year minus 90.")
        person_dates = choice(right, "Birth and death dates", PersonDateAction, defaults.person_dates)
        event_dates = choice(right, "Clinical event dates", EventDateAction, defaults.event_dates,
                             help="Dates in every resource other than the Patient: encounters, conditions, "
                                  "observations, claims, and so on. Exact dates can reveal a birth date (newborn "
                                  "visits are dated on the day of birth); coarser dates reduce that risk but limit "
                                  "time-based analysis.")
        pseudonymize_ids = right.checkbox("Replace resource ids with pseudonyms", value=defaults.pseudonymize_ids,
                                          help="References are rewritten to match, so they still resolve.")
        secret = st.text_input(
            "Pseudonym secret (optional)", type="password",
            help="Leave empty for pseudonyms that differ on every run. With a secret, the same input always gets "
                 "the same pseudonyms, so sanitized datasets can be linked. The secret is never stored.",
        )
        st.caption("Always removed: patient names, telecom, photos, contacts, birth place and mother's maiden "
                   "name extensions, narrative text, and display text on references.")
        submitted = st.form_submit_button("Generate sanitized copy", type="primary")

    if submitted:
        config = SanitizationConfig(
            identifiers=identifiers, addresses=addresses, person_dates=person_dates,
            group_ages_over_89=group_ages, event_dates=event_dates, pseudonymize_ids=pseudonymize_ids,
            secret=secret or None,
        )
        generate_sanitized_copy(result, store, config)


def generate_sanitized_copy(result: ProcessingResult, store: DatasetStore, config: SanitizationConfig) -> None:
    if dataset_fingerprint(store) != result.fingerprint:
        st.error("The dataset changed after it was processed. Process it again before generating a sanitized copy.")
        return
    try:
        with st.spinner("Sanitizing and validating the output…"):
            sanitized = sanitize_dataset(store.resources(), config, validation_before=result.analysis.validation)
    except Exception:  # noqa: BLE001 - details go to the server log only
        logger.exception("Sanitization failed")
        st.error("The sanitized copy could not be generated because of an unexpected error.", icon="🚫")
        return
    st.session_state.sanitized = (result.fingerprint, sanitized)
    if result.run_id is not None and repo() is not None:
        persist(repo().record_sanitization, result.run_id, sanitized)


def render_sanitization_result(result: SanitizationResult) -> None:
    cols = st.columns(4)
    cols[0].metric("Resources in copy", number(len(result.resources)), border=True)
    cols[1].metric("Fields changed", number(sum(result.actions.values())), border=True)
    cols[2].metric("Remaining findings", number(len(result.residual.findings)), border=True,
                   help=f"High-risk: {result.residual_high_risk:,}. Low-risk findings such as birth year or "
                        "state are kept on purpose, for analysis.")
    cols[3].metric("Errors in output", number(result.errors_after), border=True,
                   delta=f"{result.errors_after - result.errors_before:+,} vs. input", delta_color="inverse")

    problems = []
    if not result.references_preserved:
        problems.append(f"reference issues rose from {result.reference_issues_before} "
                        f"to {result.reference_issues_after}")
    if result.new_error_codes:
        problems.append("new error types: " + ", ".join(sorted(result.new_error_codes)))
    if result.residual_high_risk:
        problems.append(f"{result.residual_high_risk} high-risk findings remain")
    if problems:
        st.warning("Sanitized output needs review: " + "; ".join(problems) + ".", icon="⚠️")
    else:
        st.success("Sanitized output validated: references intact, no new error types, "
                   "no high-risk findings remaining.", icon="✅")

    with st.expander("What was changed"):
        st.dataframe(pd.DataFrame(list(result.actions.items()), columns=["Change", "Count"]), hide_index=True)
        st.json(result.config.describe(), expanded=False)

    st.download_button(
        "Download sanitized dataset (NDJSON)", result.to_ndjson(), file_name="sanitized_dataset.ndjson",
        mime="application/x-ndjson", on_click=log_event,
        args=("report.downloaded", {"report": "sanitized_dataset", "resources": len(result.resources)}),
        key="privacy_download_sanitized",
    )
