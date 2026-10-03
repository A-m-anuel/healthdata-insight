"""Audit section: processing runs, statistics, timestamps, and input/output counts."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from healthdata_insight.pipeline import ProcessingResult
from healthdata_insight.summary import format_bytes
from healthdata_insight.ui.common import current_sanitization, number, pct, persist, repo, section_title


def render_audit(result: ProcessingResult) -> None:
    validation, analytics = result.analysis.validation, result.analytics
    sanitized = current_sanitization()

    section_title("Current run", f"Run #{result.run_id if result.run_id is not None else '— (not saved)'} · "
                                 f"processed {result.processed_at:%Y-%m-%d %H:%M:%S} UTC")
    cols = st.columns(4)
    cols[0].metric("Processing time", f"{result.duration_ms:,} ms", border=True)
    cols[1].metric("Input records", number(validation.total_records), border=True,
                   help=f"{len(result.fingerprint)} file(s), {format_bytes(result.analysis.summary.total_bytes)}; "
                        f"includes {validation.malformed_record_count:,} malformed entries.")
    cols[2].metric("Analytical records out", number(analytics.record_count), border=True,
                   help=f"Excluded: {analytics.excluded_invalid:,} invalid, {analytics.excluded_duplicates:,} "
                        "duplicate; out-of-scope resource types are not analyzed.")
    cols[3].metric("Sanitized resources out", number(len(sanitized.resources)) if sanitized else "—", border=True)

    flow = pd.DataFrame([
        {"Stage": "Input files", "Count": len(result.fingerprint)},
        {"Stage": "Input records", "Count": validation.total_records},
        {"Stage": "Resources read", "Count": validation.resource_count},
        {"Stage": "Valid resources", "Count": validation.valid_resource_count},
        {"Stage": "Analytical records", "Count": analytics.record_count},
        {"Stage": "Sanitized resources", "Count": len(sanitized.resources) if sanitized else None},
    ])
    st.dataframe(flow, hide_index=True, column_config={"Count": st.column_config.NumberColumn(format="%d")})

    if repo() is None:
        st.warning("The history database is unavailable, so past runs and audit events can't be shown.", icon="⚠️")
        return
    if result.persistence_error:
        st.warning("This run could not be saved to the history database.", icon="⚠️")

    section_title("Processing runs", f"Stored in `{repo().db_path}`. Counts and scores only: no resource "
                                     "content, field values, or file names.")
    runs = persist(repo().recent_runs, 50) or []
    st.dataframe(
        pd.DataFrame([{
            "Run": r.id,
            "Processed (UTC)": r.created_at.replace("T", " ").replace("+00:00", ""),
            "Duration (ms)": r.duration_ms,
            "Files": r.file_count,
            "Input size": format_bytes(r.total_bytes),
            "Records in": r.input_records,
            "Valid": r.valid_resources,
            "Invalid": r.invalid_records,
            "Analytical out": r.analytical_records,
            "Sanitized out": r.sanitized_resources,
            "Quality": pct(r.overall_percent, 0),
            "PHI findings": r.phi_findings,
            "R": r.r_status,
        } for r in runs]),
        hide_index=True,
    )

    section_title("Audit events for this run")
    events = persist(repo().audit_events, result.run_id) if result.run_id is not None else []
    if events:
        st.dataframe(
            pd.DataFrame([{
                "Time (UTC)": e["occurred_at"].replace("T", " ").replace("+00:00", ""),
                "Event": e["event_type"],
                "Details": json.dumps(e["details"], sort_keys=True),
            } for e in events]),
            hide_index=True,
        )
    else:
        st.caption("No events recorded.")
