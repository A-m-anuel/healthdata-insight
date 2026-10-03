"""Public-health analytics section (pseudonymized analytical dataset only)."""

from __future__ import annotations

import altair as alt
import streamlit as st

from healthdata_insight.analytics import (
    AGE_GROUP_ORDER,
    GENDER_ORDER,
    AnalyticalDataset,
    CohortFilter,
    age_distribution,
    condition_frequencies,
    gender_distribution,
    geography,
    observation_statistics,
    temporal_trend,
)
from healthdata_insight.charts import age_gender_bars, gender_bar, horizontal_bar, trend_line
from healthdata_insight.pipeline import ProcessingResult
from healthdata_insight.r_charts import RStatus
from healthdata_insight.ui.common import chart_with_table, empty_state, number, section_title

# R statistics table shown under each R chart.
R_CHART_TABLES = {"condition_prevalence": "condition_prevalence_ci", "observation_distributions": "observation_mean_ci"}

ANALYTICS_BASIS = (
    "Based on the pseudonymized analytical dataset: valid, de-duplicated resources with pseudonymous keys, "
    "birth year only (ages 90+ grouped), state/country only, and clinical dates by month."
)


def cohort_filter_row(dataset: AnalyticalDataset, key: str) -> CohortFilter:
    """One filter row scoping everything below it."""
    patients = dataset.patients
    cols = st.columns(3)
    genders = cols[0].multiselect("Gender", [g for g in GENDER_ORDER if g in set(patients["gender"])],
                                  placeholder="All", key=f"{key}_gender")
    ages = cols[1].multiselect("Age group", [a for a in AGE_GROUP_ORDER if a in set(patients["age_group"].dropna())],
                               placeholder="All", key=f"{key}_age")
    states = cols[2].multiselect("State", sorted(patients["state"].dropna().unique()), placeholder="All",
                                 key=f"{key}_state")
    return CohortFilter(tuple(genders), tuple(ages), tuple(states))


def render_public_health(result: ProcessingResult) -> None:
    full = result.analytics
    st.caption(ANALYTICS_BASIS + (f" Excluded: {full.excluded_invalid:,} invalid and "
                                  f"{full.excluded_duplicates:,} duplicate resources."))
    if full.patients.empty and full.conditions.empty and full.observations.empty:
        empty_state("No valid Patient, Condition, or Observation records to analyze.")
        return

    data = cohort_filter_row(full, "ph").apply(full)
    cols = st.columns(4)
    cols[0].metric("Patients in cohort", number(len(data.patients)), border=True)
    cols[1].metric("Condition records", number(len(data.conditions)), border=True)
    cols[2].metric("Observation records", number(len(data.observations)), border=True)
    ages = data.patients["age"].dropna()
    cols[3].metric("Median age", f"{ages.median():.0f}" if len(ages) else "—", border=True,
                   help="Approximate: computed from birth year. Ages 90+ are grouped at 90.")

    demographics, conditions, observations, trends, r_tab = st.tabs(
        ["Demographics", "Conditions", "Observations", "Trends", "R analysis"])
    with demographics:
        _demographics(data)
    with conditions:
        _conditions(data)
    with observations:
        _observations(data)
    with trends:
        _trends(data)
    with r_tab:
        _r_analysis(result)


def _demographics(data: AnalyticalDataset) -> None:
    if data.patients.empty:
        empty_state("No patients match the filters.")
        return
    left, right = st.columns([2, 3], gap="large")
    with left:
        section_title("Gender")
        frame = gender_distribution(data)
        chart_with_table(gender_bar(frame), frame, key="ph_gender")
    with right:
        section_title("Age group by gender", "Age from birth year at the processing date.")
        frame = age_distribution(data)
        if frame.empty:
            st.caption("No patients with a known birth year.")
        else:
            chart_with_table(age_gender_bars(frame), frame.astype({"age_group": str}), key="ph_age")
    left, _ = st.columns([2, 3], gap="large")
    with left:
        section_title("Geography", "State of residence (the most precise location kept). Top 15.")
        frame = geography(data)
        chart_with_table(horizontal_bar(frame.head(15), "state", "patients", value_title="Patients",
                                        tooltip=["state", "patients", alt.Tooltip("share_pct:Q", format=".1f")]),
                         frame, key="ph_geo")


def _conditions(data: AnalyticalDataset) -> None:
    frame = condition_frequencies(data)
    if frame.empty:
        empty_state("No conditions match the filters.")
        return
    section_title("Condition prevalence", "Share of patients (in the current selection) with each condition. "
                                          "Top 15 shown in the chart.")
    chart_with_table(
        horizontal_bar(frame.head(15), "condition", "prevalence_pct", value_title="Patients with condition (%)",
                       value_format=".0f", domain_max=100,
                       tooltip=["condition", "code", "patients", "records",
                                alt.Tooltip("prevalence_pct:Q", title="prevalence (%)", format=".1f"), "active"]),
        frame, key="ph_conditions",
        column_config={"prevalence_pct": st.column_config.ProgressColumn(
            "Prevalence", min_value=0, max_value=100, format="%.1f%%")},
    )


def _observations(data: AnalyticalDataset) -> None:
    stats = observation_statistics(data)
    if stats.empty:
        empty_state("No numeric observation values match the filters.")
        return
    section_title("Observation statistics", "Descriptive statistics per observation type and unit.")
    st.dataframe(
        stats, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="%.1f")
                       for c in ("mean", "sd", "min", "p25", "median", "p75", "max")},
    )
    counts = stats[["observation", "n"]]
    section_title("Measurements per observation type")
    chart_with_table(horizontal_bar(counts, "observation", "n", value_title="Measurements"),
                     counts, key="ph_obs_counts")


def _trends(data: AnalyticalDataset) -> None:
    onsets = temporal_trend(data.conditions["onset_month"])
    measurements = temporal_trend(data.observations["effective_month"])
    if onsets.empty and measurements.empty:
        empty_state("No dated conditions or observations match the filters.")
        return
    left, right = st.columns(2, gap="large")
    with left:
        section_title("New conditions over time", "By onset date (monthly, or yearly for long spans).")
        if onsets.empty:
            st.caption("No conditions with an onset date.")
        else:
            chart_with_table(trend_line(onsets, value_title="Conditions"), onsets, key="ph_trend_conditions")
    with right:
        section_title("Observations over time", "By effective date.")
        if measurements.empty:
            st.caption("No observations with an effective date.")
        else:
            chart_with_table(trend_line(measurements, value_title="Observations"), measurements,
                             key="ph_trend_observations")


def _r_analysis(result: ProcessingResult) -> None:
    r = result.r_output
    if r.status is not RStatus.OK:
        st.info(r.message or "R analysis did not run.", icon="ℹ️")
        return
    st.caption(f"Generated by {r.r_version} from the full analytical dataset (the filters above do not apply). "
               "R receives only the pseudonymized extract.")
    # The first two charts side by side; the small-multiples chart gets the full width.
    for row in (r.charts[:2], r.charts[2:]):
        if not row:
            continue
        for column, chart in zip(st.columns(len(row), gap="large"), row):
            with column:
                st.image(chart.png, width="stretch", caption=None)  # the title is drawn inside the image
                table = r.tables.get(R_CHART_TABLES.get(chart.name, ""))
                if table is not None:
                    with st.expander("View statistics", expanded=False):
                        st.dataframe(table, hide_index=True, column_config={
                            c: st.column_config.NumberColumn(format="%.2f") for c in table.columns
                            if table[c].dtype.kind == "f"})
    for note in r.notes:
        st.caption(note)
