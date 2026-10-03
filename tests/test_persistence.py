from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import FIXED_NOW
from healthdata_insight.analysis import analyze_store
from healthdata_insight.ingest import ingest_file
from healthdata_insight.persistence import DB_PATH_ENV, RunRepository, default_db_path
from healthdata_insight.samples import load_samples
from healthdata_insight.sanitize import SanitizationConfig, sanitize_dataset


@pytest.fixture
def repo(tmp_path: Path) -> RunRepository:
    return RunRepository(tmp_path / "history" / "hdi.db")


def load(store, sample_set="Clean synthetic data"):
    names = []
    for name, data in load_samples(sample_set):
        ingest_file(store, name, data)
        names.append(name)
    return names


def record(repo, store):
    analysis = analyze_store(store, now=FIXED_NOW)
    run_id = repo.record_run(store.files(), analysis.summary, analysis.validation, analysis.quality,
                             analysis.privacy)
    return run_id, analysis


def test_database_is_created_with_parent_directory(repo):
    assert repo.db_path.exists()


def test_default_path_honours_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "x.db"))
    assert default_db_path() == tmp_path / "x.db"
    monkeypatch.delenv(DB_PATH_ENV)
    assert default_db_path() == Path("data") / "healthdata_insight.db"


def test_run_stores_every_result_type(repo, store):
    load(store, "Corrupted synthetic data")
    run_id, analysis = record(repo, store)

    counts = repo.table_counts(run_id)
    assert counts["run_files"] == 1
    assert counts["resource_statistics"] == len(analysis.summary.resource_counts)
    assert counts["validation_results"] == 1
    assert counts["validation_issues"] == len(analysis.validation.issues)
    assert counts["quality_results"] == 1
    assert counts["phi_findings"] == len(analysis.privacy.findings)
    assert counts["audit_events"] == 1

    [run] = repo.recent_runs()
    assert (run.id, run.resource_count, run.invalid_records) == (run_id, 23, 17)
    assert run.grade == analysis.quality.grade.value
    assert run.phi_findings == len(analysis.privacy.findings)
    assert not run.sanitized

    with sqlite3.connect(repo.db_path) as conn:
        row = conn.execute("SELECT valid_resources, error_count, warning_count FROM validation_results").fetchone()
        assert row == (9, analysis.validation.error_count, analysis.validation.warning_count)
        stats = dict(conn.execute("SELECT resource_type, resource_count FROM resource_statistics"))
        assert stats["Patient"] == analysis.summary.resource_counts["Patient"]
        finding = conn.execute("SELECT resource_type, path, category, severity, method, rule "
                               "FROM phi_findings LIMIT 1").fetchone()
        assert finding[0] == "Patient" and finding[1].startswith("Patient.") and finding[4] == "fhir-field-rule"


def test_sanitization_and_audit_events(repo, store):
    load(store)
    run_id, analysis = record(repo, store)
    result = sanitize_dataset(store.resources(), SanitizationConfig(secret="s3cret"),
                              validation_before=analysis.validation, now=FIXED_NOW)
    repo.record_sanitization(run_id, result)
    repo.log_event("report.downloaded", {"report": "sanitized_dataset"}, run_id)
    repo.log_event("dataset.cleared")

    assert repo.table_counts(run_id)["sanitization_results"] == 1
    assert repo.recent_runs()[0].sanitized
    events = repo.audit_events()
    assert [e["event_type"] for e in events] == [
        "run.completed", "sanitization.generated", "report.downloaded", "dataset.cleared"]
    assert events[1]["details"]["config"]["stable_pseudonyms"] is True
    assert events[-1]["run_id"] is None
    assert len(repo.audit_events(run_id)) == 3


def test_no_raw_phi_or_identifiers_are_stored(repo, store):
    """Scan the whole database file for values from the source data."""
    file_names = load(store)
    run_id, analysis = record(repo, store)
    result = sanitize_dataset(store.resources(), SanitizationConfig(secret="s3cret"),
                              validation_before=analysis.validation, now=FIXED_NOW)
    repo.record_sanitization(run_id, result)

    sensitive: set[str] = set(file_names) | {"s3cret"}
    for stored in store.resources():
        r = stored.resource
        sensitive.add(r["id"])
        if r["resourceType"] != "Patient":
            continue
        sensitive.add(r["birthDate"])
        sensitive |= {n["family"] for n in r["name"]}
        sensitive |= {t["value"] for t in r["telecom"]}
        sensitive |= {i["value"] for i in r["identifier"]}
        sensitive |= {a["postalCode"] for a in r["address"]} | {a["line"][0] for a in r["address"]}

    with sqlite3.connect(repo.db_path) as conn:
        text_values = [
            value
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            for row in conn.execute(f"SELECT * FROM {table}")
            for value in row
            if isinstance(value, str)
        ]
    database_text = "\n".join(text_values)
    leaked = sorted(v for v in sensitive if v in database_text)
    assert leaked == []


def test_resource_refs_link_within_a_run_but_not_across_runs(repo, store):
    load(store, "Corrupted synthetic data")
    first, _ = record(repo, store)
    second, _ = record(repo, store)
    with sqlite3.connect(repo.db_path) as conn:
        def refs(run_id, table):
            return {r for (r,) in conn.execute(f"SELECT resource_ref FROM {table} WHERE run_id = ? "
                                               "AND resource_type = 'Patient'", (run_id,)) if r}
        # The same Patient appears in both validation issues and PHI findings of a run...
        assert refs(first, "validation_issues") & refs(first, "phi_findings")
        # ...but refs differ between runs of the same data.
        assert not refs(first, "phi_findings") & refs(second, "phi_findings")


def test_run_is_atomic(repo, store, monkeypatch):
    load(store)
    analysis = analyze_store(store, now=FIXED_NOW)
    bad_privacy = type(analysis.privacy)(findings=(None,), resources_scanned=0)  # breaks mid-transaction
    with pytest.raises(AttributeError):
        repo.record_run(store.files(), analysis.summary, analysis.validation, analysis.quality, bad_privacy)
    assert repo.recent_runs() == []
    assert repo.audit_events() == []
