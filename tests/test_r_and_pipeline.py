from __future__ import annotations

import io
import json
import sqlite3
import sys
import zipfile

import pytest

from conftest import FIXED_NOW
from healthdata_insight import reports
from healthdata_insight.analytics import observation_statistics
from healthdata_insight.ingest import ingest_file
from healthdata_insight.parser import parse_fhir_file
from healthdata_insight.persistence import RunRepository
from healthdata_insight.pipeline import process_dataset
from healthdata_insight.r_charts import RSCRIPT_ENV, RStatus, find_rscript, run_r_analysis
from healthdata_insight.samples import load_samples
from healthdata_insight.sanitize import SanitizationConfig, sanitize_dataset

requires_r = pytest.mark.skipif(find_rscript() is None, reason="R is not installed")


@pytest.fixture
def repo(tmp_path):
    return RunRepository(tmp_path / "history.db")


def load(store, sample_set="Clean synthetic data"):
    for name, data in load_samples(sample_set):
        ingest_file(store, name, data)


# --------------------------------------------------------------------------- R

def test_r_unavailable(monkeypatch, store):
    monkeypatch.setenv(RSCRIPT_ENV, "C:/does/not/exist/Rscript.exe")
    assert find_rscript() is None
    load(store)
    result = process_dataset(store, None, now=FIXED_NOW)
    assert result.r_output.status is RStatus.UNAVAILABLE
    assert "not installed" in result.r_output.message


def test_r_failure_is_reported_not_raised(store):
    load(store)
    analytics = process_dataset(store, None, use_r=False, now=FIXED_NOW).analytics
    output = run_r_analysis(analytics, rscript=sys.executable)  # not R: the script can't run
    assert output.status is RStatus.FAILED
    assert output.charts == ()


@requires_r
@pytest.mark.parametrize("sample_set", ["Clean synthetic data", "Corrupted synthetic data"])
def test_r_charts_and_statistics(store, sample_set):
    load(store, sample_set)
    result = process_dataset(store, None, now=FIXED_NOW)
    r = result.r_output
    assert r.status is RStatus.OK, r.message
    assert r.r_version.startswith("R version")
    assert [c.name for c in r.charts] == ["age_gender", "condition_prevalence", "observation_distributions"]
    assert all(c.png.startswith(b"\x89PNG") for c in r.charts)

    prevalence = r.tables["condition_prevalence_ci"]
    assert (prevalence["ci_low_pct"] <= prevalence["prevalence_pct"]).all()
    assert (prevalence["prevalence_pct"] <= prevalence["ci_high_pct"]).all()
    assert (prevalence["total"] == len(result.analytics.patients)).all()


@requires_r
def test_r_matches_python_statistics(store):
    load(store)
    result = process_dataset(store, None, now=FIXED_NOW)
    r_means = result.r_output.tables["observation_mean_ci"].set_index("observation")["mean"]
    py_means = observation_statistics(result.analytics).set_index("observation")["mean"]
    for observation, mean in r_means.items():
        assert mean == pytest.approx(py_means[observation])
    # Exact binomial CI for 10 of 18 patients (diabetes in the sample).
    row = result.r_output.tables["condition_prevalence_ci"].iloc[0]
    assert (row["patients"], row["total"]) == (10, 18)
    assert (row["ci_low_pct"], row["ci_high_pct"]) == pytest.approx((30.76, 78.47), abs=0.01)


@requires_r
def test_r_handles_empty_input(store):
    store.add_file(parse_fhir_file("e.json", b'{"resourceType":"Encounter","id":"e1"}'))
    r = process_dataset(store, None, now=FIXED_NOW).r_output
    assert r.status is RStatus.OK
    assert r.charts == () and len(r.notes) == 3  # each plot skipped with a reason


# --------------------------------------------------------------------------- pipeline + persistence

def test_pipeline_persists_run_with_processing_stats(store, repo):
    load(store)
    result = process_dataset(store, repo, use_r=False, now=FIXED_NOW)
    [run] = repo.recent_runs()
    assert run.id == result.run_id
    assert (run.input_records, run.resource_count, run.valid_resources) == (127, 127, 127)
    assert run.analytical_records == result.analytics.record_count == 126  # the Encounter is not analyzed
    assert run.duration_ms == result.duration_ms >= 0
    assert run.r_status == "unavailable"
    assert result.files[0].sha256 == result.fingerprint[0]


def test_pipeline_survives_database_failure(store, repo, monkeypatch):
    load(store)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(repo, "record_run", broken)
    result = process_dataset(store, repo, use_r=False, now=FIXED_NOW)
    assert result.run_id is None and "disk I/O error" in result.persistence_error
    assert result.analysis.summary.total_resources == 127


def test_schema_migration_from_version_1(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE schema_info (version INTEGER NOT NULL);
            INSERT INTO schema_info VALUES (1);
            CREATE TABLE processing_runs (id INTEGER PRIMARY KEY, run_uuid TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, app_version TEXT NOT NULL, file_count INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL, resource_count INTEGER NOT NULL);
            INSERT INTO processing_runs VALUES (1, 'u', '2026-01-01T00:00:00+00:00', '0.1.0', 1, 10, 5);
        """)
    repo = RunRepository(path)
    assert repo.schema_version == 2
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(processing_runs)")}
        assert {"input_records", "duration_ms", "analytical_records", "r_status"} <= columns
        assert conn.execute("SELECT resource_count, duration_ms FROM processing_runs").fetchone() == (5, None)


# --------------------------------------------------------------------------- reports

def test_reports(store):
    load(store)
    result = process_dataset(store, None, now=FIXED_NOW)
    quality = json.loads(reports.to_json(reports.quality_report(result)))
    assert quality["overall"]["grade"] == "Excellent"
    assert {d["dimension"] for d in quality["dimensions"]} == {"completeness", "validity", "consistency",
                                                               "uniqueness"}
    validation = json.loads(reports.to_json(reports.validation_report(result)))
    assert validation["summary"]["valid_resources"] == 127
    privacy = json.loads(reports.to_json(reports.privacy_report(result, None)))
    assert privacy["summary"]["potential_phi_findings"] == len(result.analysis.privacy.findings)
    assert privacy["sanitization"]["status"] == "not generated"
    assert "HIPAA" in privacy["disclaimer"]
    summary = json.loads(reports.to_json(reports.analytical_summary(result)))
    assert summary["records"]["patients"] == 18
    assert summary["conditions"][0]["patients"] >= summary["conditions"][-1]["patients"]
    assert reports.validation_issues_csv(result).startswith(b"Severity,")


def test_report_bundle_and_no_phi_in_any_report(store):
    load(store, "Corrupted synthetic data")
    load(store)
    result = process_dataset(store, None, now=FIXED_NOW)
    sanitized = sanitize_dataset(store.resources(), SanitizationConfig(), validation_before=result.analysis.validation)

    bundle = zipfile.ZipFile(io.BytesIO(reports.report_bundle(result, sanitized)))
    assert set(bundle.namelist()) == {
        "data_quality_report.json", "validation_report.json", "validation_issues.csv", "privacy_report.json",
        "analytical_summary.json", "sanitized_dataset.ndjson", "sanitization_settings.json"}
    privacy = json.loads(bundle.read("privacy_report.json"))
    assert privacy["sanitization"]["status"] == "generated"
    assert privacy["sanitization"]["references_preserved"] is True

    sensitive = {name for name, _ in load_samples("Corrupted synthetic data")} | \
                {name for name, _ in load_samples()}
    for stored in store.resources():
        r = stored.resource
        if r["resourceType"] != "Patient":
            continue
        for key in ("birthDate", "deceasedDateTime"):
            if isinstance(r.get(key), str):
                sensitive.add(r[key])
        sensitive |= {n["family"] for n in r.get("name", []) if n.get("family")}
        sensitive |= {t["value"] for t in r.get("telecom", [])}
        sensitive |= {i["value"] for i in r.get("identifier", []) if i.get("value")}
        sensitive |= {a.get("postalCode") for a in r.get("address", []) if a.get("postalCode")}
    for name in bundle.namelist():
        if name == "sanitized_dataset.ndjson":
            continue
        text = bundle.read(name).decode("utf-8")
        leaked = sorted(v for v in sensitive if v in text)
        assert leaked == [], (name, leaked)
