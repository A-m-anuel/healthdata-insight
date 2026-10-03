"""Data explorer: filter and inspect the pseudonymized analytical dataset.

Only the analytical dataset is shown here - never the uploaded resources - so
no raw PHI can appear: keys are pseudonyms, birth dates are years, locations
are states, dates are months, and free text is not extracted at all.
"""

from __future__ import annotations

import streamlit as st

from healthdata_insight.pipeline import ProcessingResult
from healthdata_insight.reports import csv_bytes
from healthdata_insight.ui.common import empty_state, log_event
from healthdata_insight.ui.public_health import cohort_filter_row

TABLES = ("Patients", "Conditions", "Observations")


def render_explorer(result: ProcessingResult) -> None:
    full = result.analytics
    st.caption("Pseudonymized analytical data: pseudonymous keys (new for every processing run), birth year, "
               "state, and month-level dates. Names, identifiers, contact details, and free text are never "
               "included.")
    table = st.segmented_control("Table", TABLES, default=TABLES[0], key="explorer_table", required=True)
    data = cohort_filter_row(full, "ex").apply(full)

    if table == "Patients":
        frame = data.patients
    elif table == "Conditions":
        frame = data.conditions
        chosen = st.multiselect("Condition", sorted(frame["condition"].dropna().unique()), placeholder="All",
                                key="ex_condition")
        if chosen:
            frame = frame[frame["condition"].isin(chosen)]
    else:
        frame = data.observations
        cols = st.columns([3, 1])
        chosen = cols[0].multiselect("Observation", sorted(frame["observation"].dropna().unique()),
                                     placeholder="All", key="ex_observation")
        numeric_only = cols[1].toggle("Numeric values only", key="ex_numeric")
        if chosen:
            frame = frame[frame["observation"].isin(chosen)]
        if numeric_only:
            frame = frame[frame["value"].notna()]

    if frame.empty:
        empty_state("No records match the filters.")
        return
    st.caption(f"{len(frame):,} {table.lower()}")
    st.dataframe(frame, hide_index=True, height=420, key=f"ex_table_{table}",
                 column_config={"value": st.column_config.NumberColumn(format="%.2f")})
    st.download_button(
        f"Download filtered {table.lower()} (CSV)", csv_bytes(frame),
        file_name=f"analytical_{table.lower()}.csv", mime="text/csv", on_click=log_event,
        args=("report.downloaded", {"report": f"analytical_{table.lower()}", "rows": len(frame)}),
        key="ex_download",
    )
