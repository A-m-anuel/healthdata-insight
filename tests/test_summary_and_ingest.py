from __future__ import annotations

from conftest import resource, to_bytes, to_ndjson
from healthdata_insight.ingest import IngestStatus, ingest_file
from healthdata_insight.samples import load_samples
from healthdata_insight.summary import (
    files_frame,
    format_bytes,
    resource_counts_frame,
    summarize_store,
)


def test_ingest_adds_then_recognises_duplicate(store, bundle):
    data = to_bytes(bundle)
    first = ingest_file(store, "a.json", data)
    second = ingest_file(store, "copy.json", data)
    assert first.status is IngestStatus.ADDED
    assert first.resource_count == 5
    assert second.status is IngestStatus.ALREADY_LOADED
    assert len(store.files()) == 1


def test_ingest_failure_does_not_touch_store(store):
    result = ingest_file(store, "bad.json", b"{oops")
    assert result.status is IngestStatus.FAILED
    assert "Invalid JSON" in result.message
    assert store.files() == []


def test_summary_across_files(store, bundle):
    ingest_file(store, "bundle.json", to_bytes(bundle))
    ingest_file(store, "more.ndjson", to_ndjson([
        resource("Patient", "p1"),  # same id as in the bundle
        resource("Encounter", "e1"),
    ]) + b"\nbad")

    summary = summarize_store(store)
    assert summary.file_count == 2
    assert summary.total_resources == 7
    assert summary.resource_type_count == 4
    assert summary.bundle_count == 1
    assert summary.parse_issue_count == 1
    assert summary.distinct_patients == 2
    assert summary.in_scope_counts == {"Patient": 3, "Condition": 1, "Observation": 2}
    assert summary.out_of_scope_types == ["Encounter"]
    # Sorted by descending count, then name.
    assert list(summary.resource_counts) == ["Patient", "Observation", "Condition", "Encounter"]


def test_frames(store, bundle):
    ingest_file(store, "bundle.json", to_bytes(bundle))
    summary = summarize_store(store)

    counts = resource_counts_frame(summary)
    assert list(counts.columns) == ["Resource type", "Count", "Share (%)", "In scope"]
    assert counts["Count"].sum() == 5
    assert round(counts["Share (%)"].sum(), 6) == 100
    assert counts["In scope"].all()

    files = files_frame(store.files())
    assert files.loc[0, "File"] == "bundle.json"
    assert files.loc[0, "Resources"] == 5


def test_empty_frames(store):
    summary = summarize_store(store)
    assert summary.total_resources == 0
    assert resource_counts_frame(summary).empty
    assert files_frame([]).empty


def test_format_bytes():
    assert format_bytes(512) == "512 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_bundled_samples_are_valid(store):
    for name, data in load_samples():
        result = ingest_file(store, name, data)
        assert result.status is IngestStatus.ADDED, result.message
        assert result.issues == ()
    summary = summarize_store(store)
    assert all(count > 0 for count in summary.in_scope_counts.values())
