"""Shared Streamlit helpers: session state, persistence guard, formatting, chart + table pairs."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any, TypeVar

import altair as alt
import pandas as pd
import streamlit as st

from healthdata_insight.models import QualityGrade
from healthdata_insight.persistence import RunRepository
from healthdata_insight.pipeline import ProcessingResult
from healthdata_insight.sanitize import SanitizationResult

logger = logging.getLogger(__name__)
T = TypeVar("T")

GRADE_STYLE = {
    QualityGrade.EXCELLENT: st.success,
    QualityGrade.GOOD: st.success,
    QualityGrade.FAIR: st.warning,
    QualityGrade.POOR: st.error,
    QualityGrade.NOT_RATED: st.info,
}


def repo() -> RunRepository | None:
    return st.session_state.get("repo")


def current_result() -> ProcessingResult | None:
    return st.session_state.get("result")


def current_sanitization() -> SanitizationResult | None:
    result, cached = current_result(), st.session_state.get("sanitized")
    return cached[1] if result and cached and cached[0] == result.fingerprint else None


def persist(fn: Callable[..., T], *args: Any) -> T | None:
    """Call a repository method; history problems must never break the analysis itself."""
    if repo() is None:
        return None
    try:
        return fn(*args)
    except (sqlite3.Error, OSError):
        logger.exception("Could not write to the history database")
        st.warning("Results could not be saved to the history database.", icon="⚠️")
        return None


def log_event(event_type: str, details: dict[str, Any] | None = None) -> None:
    """Record an audit event against the current run. ``details`` must never contain PHI."""
    if repo() is not None:
        result = current_result()
        persist(repo().log_event, event_type, details, result.run_id if result else None)


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")


def md_escape(text: str) -> str:
    """Escape user-controlled text (e.g. uploaded file names) before it goes into Markdown.

    Without this, a file named ``![x](https://example.org/p.png)`` would make the browser fetch a
    remote image, and other names could inject links or formatting.
    """
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}%"


def number(value: int | float | None) -> str:
    return "—" if value is None else f"{value:,}"


def chart_with_table(chart: alt.Chart, table: pd.DataFrame, *, key: str,
                     column_config: dict | None = None) -> None:
    """Every chart ships with a table view of the same data (accessibility and exact values)."""
    st.altair_chart(chart, theme=None, width="stretch", key=f"chart_{key}")
    with st.expander("View data", expanded=False):
        st.dataframe(table, hide_index=True, column_config=column_config, key=f"table_{key}")


def section_title(title: str, caption: str | None = None) -> None:
    st.markdown(f"#### {title}")
    if caption:
        st.caption(caption)


def empty_state(message: str) -> None:
    st.info(message, icon="ℹ️")
