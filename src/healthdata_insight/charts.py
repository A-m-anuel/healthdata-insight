"""Altair chart builders for the dashboard.

Design rules (shared with the R charts): one colour for single-series charts,
categorical colours from a fixed, colour-blind-validated order and bound to
the entity (a gender always gets the same colour), thin marks with rounded
data ends, hairline grids, tooltips on every mark, and one y-axis per chart.
Two of the categorical colours are below 3:1 contrast on the light surface,
so every chart is shown with a table view.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

from healthdata_insight.analytics import AGE_GROUP_ORDER, GENDER_ORDER

# Validated categorical order (light mode) and chart chrome.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
PRIMARY = SERIES[0]
GENDER_COLORS = dict(zip(GENDER_ORDER, SERIES))
INK, MUTED, GRID, BASELINE = "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
ROW_HEIGHT = 32


def _style(chart: alt.Chart, height: int) -> alt.Chart:
    return (
        chart.properties(height=height)
        .configure(font=FONT, background="transparent")
        .configure_view(strokeWidth=0)
        .configure_axis(
            gridColor=GRID, gridWidth=1, domain=False, ticks=False, labelPadding=6,
            labelColor=MUTED, titleColor=INK, labelFontSize=11, titleFontSize=11, titleFontWeight="normal",
        )
        .configure_axisBand(grid=False)  # categories never get gridlines; value axes do
        .configure_legend(labelColor=INK, titleColor=INK, orient="top", symbolType="square", labelFontSize=11)
    )


def horizontal_bar(
    frame: pd.DataFrame,
    category: str,
    value: str,
    *,
    value_title: str,
    value_format: str = ",.0f",
    domain_max: float | None = None,
    tooltip: list | None = None,
    largest_first: bool = True,
) -> alt.Chart:
    """Single-series horizontal bar chart, largest first (or smallest first)."""
    scale = alt.Scale(domain=[0, domain_max]) if domain_max is not None else alt.Undefined
    chart = alt.Chart(frame).mark_bar(color=PRIMARY, cornerRadiusEnd=4, height={"band": 0.65}).encode(
        y=alt.Y(f"{category}:N", sort="-x" if largest_first else "x", title=None, axis=alt.Axis(labelLimit=260)),
        x=alt.X(f"{value}:Q", title=value_title, scale=scale, axis=alt.Axis(format=value_format, tickCount=5)),
        tooltip=tooltip or [alt.Tooltip(f"{category}:N"), alt.Tooltip(f"{value}:Q", format=value_format)],
    )
    return _style(chart, max(len(frame), 1) * ROW_HEIGHT + 40)


def gender_bar(frame: pd.DataFrame) -> alt.Chart:
    chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=4, height={"band": 0.65}).encode(
        y=alt.Y("gender:N", sort=GENDER_ORDER, title=None),
        x=alt.X("patients:Q", title="Patients", axis=alt.Axis(tickMinStep=1, format="d")),
        color=alt.Color("gender:N", scale=_gender_scale(), legend=None),
        tooltip=["gender:N", "patients:Q", alt.Tooltip("share_pct:Q", title="share (%)", format=".1f")],
    )
    return _style(chart, len(frame) * ROW_HEIGHT + 40)


def age_gender_bars(frame: pd.DataFrame) -> alt.Chart:
    """Grouped columns: patients per age group, one column per gender (fixed gender colours)."""
    data = frame.astype({"age_group": str})
    chart = alt.Chart(data).mark_bar(cornerRadiusEnd=4, width={"band": 0.9}).encode(
        x=alt.X("age_group:N", sort=AGE_GROUP_ORDER, scale=alt.Scale(domain=AGE_GROUP_ORDER), title="Age group",
                axis=alt.Axis(labelAngle=0)),
        xOffset=alt.XOffset("gender:N", sort=GENDER_ORDER, scale=alt.Scale(paddingInner=0.08)),
        y=alt.Y("patients:Q", title="Patients", axis=alt.Axis(tickMinStep=1, format="d")),
        color=alt.Color("gender:N", scale=_gender_scale(), title=None, sort=GENDER_ORDER),
        tooltip=["age_group:N", "gender:N", "patients:Q"],
    )
    return _style(chart, 260)


def trend_line(frame: pd.DataFrame, *, value_title: str) -> alt.Chart:
    granularity = frame["granularity"].iloc[0] if len(frame) else "month"
    time_format = "%Y" if granularity == "year" else "%b %Y"
    base = alt.Chart(frame).encode(
        x=alt.X("period:T", title=None, axis=alt.Axis(format=time_format, labelAngle=0, tickCount=8, grid=False)),
        y=alt.Y("count:Q", title=value_title, axis=alt.Axis(tickMinStep=1, format="d")),
        tooltip=[alt.Tooltip("period:T", title=granularity, format=time_format), alt.Tooltip("count:Q")],
    )
    line = base.mark_line(color=PRIMARY, strokeWidth=2)  # straight segments: counts are not continuous
    # Invisible wide points give a generous hover target; visible markers only on hover.
    hover = alt.selection_point(on="pointerover", nearest=True, fields=["period"], empty=False)
    points = base.mark_point(color=PRIMARY, filled=True, size=70, stroke="#fcfcfb", strokeWidth=2).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
    ).add_params(hover)
    return _style(alt.layer(line, points), 220)


def _gender_scale() -> alt.Scale:
    return alt.Scale(domain=GENDER_ORDER, range=[GENDER_COLORS[g] for g in GENDER_ORDER])
