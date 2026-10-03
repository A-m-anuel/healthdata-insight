from __future__ import annotations

import gc
import json

from conftest import resource, to_bytes, to_ndjson
from healthdata_insight.parser import parse_fhir_file
from healthdata_insight.storage import DatasetStore


def test_add_file_and_counts(store, bundle):
    parsed = parse_fhir_file("bundle.json", to_bytes(bundle))
    store.add_file(parsed)

    assert store.has_file(parsed.sha256)
    assert store.resource_counts() == {"Patient": 2, "Condition": 1, "Observation": 2}
    assert store.distinct_resource_ids("Patient") == 2

    [record] = store.files()
    assert record.name == "bundle.json"
    assert record.format == "JSON"
    assert record.resource_count == 5
    assert record.bundle_count == 1
    assert record.parse_issue_count == 0


def test_resources_are_stored_as_json(store):
    patient = resource("Patient", "p1", gender="female")
    store.add_file(parse_fhir_file("p.json", to_bytes(patient)))
    with store._connect() as conn:
        (payload,) = conn.execute("SELECT payload FROM resources").fetchone()
    assert json.loads(payload) == patient


def test_parse_issues_are_recorded(store):
    store.add_file(parse_fhir_file("x.ndjson", b'{"resourceType":"Patient"}\nbad\n'))
    [record] = store.files()
    assert record.parse_issue_count == 1
    [warning] = store.parse_issues()
    assert (warning.file_name, warning.line) == ("x.ndjson", 2)


def test_retain_only_removes_other_files_and_their_rows(store, bundle):
    keep = parse_fhir_file("bundle.json", to_bytes(bundle))
    drop = parse_fhir_file("extra.ndjson", to_ndjson([resource("Encounter", "e1")]) + b"\nbad")
    store.add_file(keep)
    store.add_file(drop)

    assert store.retain_only([keep.sha256]) == 1
    assert [f.name for f in store.files()] == ["bundle.json"]
    assert "Encounter" not in store.resource_counts()
    assert store.parse_issues() == []


def test_retain_only_empty_clears_everything(store, bundle):
    store.add_file(parse_fhir_file("bundle.json", to_bytes(bundle)))
    store.retain_only([])
    assert store.files() == []
    assert store.resource_counts() == {}


def test_temporary_store_cleanup(bundle):
    store = DatasetStore.create_temporary()
    store.add_file(parse_fhir_file("bundle.json", to_bytes(bundle)))
    directory = store.db_path.parent
    assert store.db_path.exists()
    store.cleanup()
    assert not directory.exists()


def test_temporary_store_removed_when_collected():
    store = DatasetStore.create_temporary()
    directory = store.db_path.parent
    del store
    gc.collect()
    assert not directory.exists()


def test_retain_only_handles_more_files_than_sqlite_parameter_limits(store):
    for n in range(1_200):  # more than the 999-parameter limit of older SQLite builds
        store.add_file(parse_fhir_file(f"p{n}.json", to_bytes(resource("Patient", f"p{n}"))))
    files = store.files()
    keep = [f.sha256 for f in files[:1_100]]
    assert store.retain_only(keep) == 100
    assert len(store.files()) == 1_100
    assert store.resource_counts() == {"Patient": 1_100}
