"""End-to-end tests that drive the Streamlit dashboard headlessly through the full workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from healthdata_insight.persistence import DB_PATH_ENV, RunRepository
from healthdata_insight.r_charts import find_rscript

APP = Path(__file__).resolve().parents[1] / "app.py"
SECTIONS = ["Overview", "Data quality", "FHIR validation", "Privacy", "Public health", "Data explorer", "Audit",
            "Reports"]


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "history.db"
    monkeypatch.setenv(DB_PATH_ENV, str(path))
    return path


@pytest.fixture
def app(db_path) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    yield at
    at.session_state.store.cleanup()


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def button(at: AppTest, label: str):
    [match] = [b for b in at.button if b.label == label]
    return match


def table_with(at: AppTest, *columns: str):
    """The dataframe whose columns include all of ``columns``."""
    [match] = [d.value for d in at.dataframe if set(columns) <= set(d.value.columns)]
    return match


def process(at: AppTest, sample_set: str) -> AppTest:
    at.selectbox(key="sample_set").set_value(sample_set).run()
    button(at, "Process dataset").click().run()
    assert not at.exception, at.exception
    return at


def test_welcome_page_guides_the_workflow(app):
    assert not app.exception
    assert app.title[0].value == "HealthData Insight"
    assert not app.metric and not app.tabs
    assert button(app, "Process dataset").disabled  # nothing to process yet
    assert any("Upload FHIR R4 files" in i.value for i in app.info)


def test_files_are_staged_until_processed(app):
    app.selectbox(key="sample_set").set_value("Clean synthetic data").run()
    assert not app.tabs
    assert not button(app, "Process dataset").disabled
    assert any("Click **Process dataset**" in i.value for i in app.info)


def test_full_dashboard_clean_sample(app, db_path):
    process(app, "Clean synthetic data")
    assert set(SECTIONS) <= {t.label for t in app.tabs}
    m = metrics(app)
    # Overview
    assert (m["Total patients"], m["Total resources"], m["Conditions"], m["Observations"]) == ("18", "127", "34", "74")
    assert re.fullmatch(r"\d{2}:\d{2}", m["Processed (UTC)"])
    # Data quality
    assert (m["Completeness"], m["Validity"], m["Duplicates"], m["Missing values"], m["Validation errors"]) == (
        "100.0%", "100.0%", "0", "0", "0")
    # FHIR validation
    assert (m["Valid resources"], m["Invalid resources"]) == ("127", "0")
    # Privacy
    assert int(m["Potential PHI findings"]) > 0 and m["Resources affected"] == "18"
    assert m["Sanitization"] == "Not yet"
    # Public health
    assert m["Patients in cohort"] == "18"
    # Audit
    assert m["Input records"] == "127" and m["Analytical records out"] == "126"
    # Reports (the sanitized copy isn't generated yet)
    labels = {d.label for d in app.download_button}
    assert {"Download data-quality report", "JSON", "Issues CSV", "Download privacy report",
            "Download analytical summary", "Download all (ZIP)"} <= labels

    [run] = RunRepository(db_path).recent_runs()
    assert run.resource_count == 127 and run.analytical_records == 126


@pytest.mark.skipif(find_rscript() is None, reason="R is not installed")
def test_r_visualizations_are_shown(app):
    process(app, "Clean synthetic data")
    assert len(app.image) == 3


def test_corrupted_sample_dashboard(app):
    process(app, "Corrupted synthetic data")
    m = metrics(app)
    assert (m["Valid resources"], m["Invalid resources"], m["Duplicates"], m["Missing values"]) == ("9", "17", "4",
                                                                                                    "13")
    assert m["Validity"] == "34.6%"
    assert m["Patients in cohort"] == "2"  # analytics use only valid, de-duplicated records
    assert any("Most frequent issues" in w.value for w in app.warning)


def test_validation_filters(app):
    process(app, "Corrupted synthetic data")
    app.multiselect(key="val_severity").set_value(["error"]).run()
    frame = table_with(app, "Severity", "Dimension", "Message")
    assert set(frame["Severity"]) == {"error"}


def test_public_health_filter_scopes_the_section(app):
    process(app, "Clean synthetic data")
    app.multiselect(key="ph_gender").set_value(["male"]).run()
    assert not app.exception
    male = int(metrics(app)["Patients in cohort"])
    assert 0 < male < 18


def test_data_explorer_never_shows_raw_phi(app):
    process(app, "Clean synthetic data")
    # Short numeric values (postal codes, phone numbers) can occur by chance inside random pseudonym
    # hex, so those are compared against whole cell values; names and dates can't, so substrings are checked.
    exact, substrings = set(), set()
    for stored in app.session_state.store.resources():
        r = stored.resource
        exact.add(r["id"])
        if r["resourceType"] == "Patient":
            exact |= {r["telecom"][0]["value"], r["identifier"][0]["value"], r["address"][0]["postalCode"]}
            substrings |= {r["birthDate"], r["name"][0]["family"], r["address"][0]["city"]}
    for table in ("Patients", "Conditions", "Observations"):
        app.segmented_control(key="explorer_table").set_value(table).run()
        assert not app.exception
        key_column = {"Patients": "birth_year", "Conditions": "condition_key", "Observations": "observation_key"}
        frame = table_with(app, key_column[table])
        assert len(frame) > 0
        cells = {str(v) for v in frame.to_numpy().ravel()}
        text = " ".join(cells)
        assert exact & cells == set(), table
        assert [v for v in substrings if v in text] == [], table


def test_generate_sanitized_copy_from_privacy_section(app, db_path):
    process(app, "Clean synthetic data")
    app.toggle(key="sanitize_enabled").set_value(True).run()
    button(app, "Generate sanitized copy").click().run()
    assert not app.exception
    m = metrics(app)
    assert m["Sanitization"] == "Generated" and m["Resources in copy"] == "127"
    assert m["Sanitized resources out"] == "127"
    assert any("Sanitized output validated" in s.value for s in app.success)
    assert "Download sanitized dataset" in {d.label for d in app.download_button}
    assert RunRepository(db_path).recent_runs()[0].sanitized_resources == 127


def test_generate_sanitized_copy_from_reports_section(app):
    process(app, "Clean synthetic data")
    button(app, "Generate with default settings").click().run()
    assert not app.exception
    assert metrics(app)["Sanitization"] == "Generated"


def test_changed_files_mark_results_stale(app):
    process(app, "Clean synthetic data")
    app.selectbox(key="sample_set").set_value("Corrupted synthetic data").run()
    assert any("changed after this analysis" in w.value for w in app.warning)
    assert metrics(app)["Total resources"] == "127"  # still the processed snapshot
    button(app, "Process dataset").click().run()
    assert metrics(app)["Total resources"] == "23"


def test_audit_records_runs_and_events(app, db_path):
    process(app, "Clean synthetic data")
    process(app, "Corrupted synthetic data")
    repo = RunRepository(db_path)
    runs = repo.recent_runs()
    assert [r.resource_count for r in runs] == [23, 127]
    events = repo.audit_events(runs[0].id)
    assert [e["event_type"] for e in events] == ["run.completed"]
    assert json.dumps(events)


def test_clear_dataset_returns_to_welcome(app):
    process(app, "Clean synthetic data")
    button(app, "Clear dataset").click().run()
    assert not app.exception
    assert not app.tabs and not app.metric
    assert app.session_state.store.files() == []


def test_unwritable_history_does_not_break_the_app(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.setenv(DB_PATH_ENV, str(blocker / "history.db"))
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    process(at, "Clean synthetic data")
    assert metrics(at)["Total patients"] == "18"
    assert any("history database is unavailable" in w.value for w in at.warning)
    at.session_state.store.cleanup()


def test_sanitization_form_starts_on_the_default_settings(app):
    from healthdata_insight.sanitize import SanitizationConfig

    process(app, "Clean synthetic data")
    app.toggle(key="sanitize_enabled").set_value(True).run()
    defaults = SanitizationConfig()
    chosen = {s.label: s.value for s in app.selectbox}
    assert chosen["Patient identifiers"] is defaults.identifiers
    assert chosen["Patient addresses"] is defaults.addresses
    assert chosen["Birth and death dates"] is defaults.person_dates
    assert chosen["Clinical event dates"] is defaults.event_dates
