from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from healthdata_insight.models import ValidationReport
from healthdata_insight.storage import DatasetStore, StoredResource
from healthdata_insight.validation import validate_dataset


def resource(resource_type: str, rid: str, **extra) -> dict:
    return {"resourceType": resource_type, "id": rid, **extra}


def to_bytes(value) -> bytes:
    return json.dumps(value).encode("utf-8")


def to_ndjson(values: list) -> bytes:
    return "\n".join(json.dumps(v) for v in values).encode("utf-8")


@pytest.fixture
def bundle() -> dict:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"resource": resource("Patient", "p1")},
            {"resource": resource("Patient", "p2")},
            {"resource": resource("Condition", "c1", subject={"reference": "Patient/p1"})},
            {"resource": resource("Observation", "o1", subject={"reference": "Patient/p1"})},
            {"resource": resource("Observation", "o2", subject={"reference": "Patient/p2"})},
        ],
    }


@pytest.fixture
def store(tmp_path: Path) -> DatasetStore:
    return DatasetStore(tmp_path / "test.db")


# --------------------------------------------------------------------------- validation helpers

FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def stored(*resources: dict, file_name: str = "test.ndjson") -> list[StoredResource]:
    return [StoredResource(key, file_name, r) for key, r in enumerate(resources, start=1)]


def validate(*resources: dict, parse_issues=()) -> ValidationReport:
    return validate_dataset(stored(*resources), parse_issues, now=FIXED_NOW)


def codes(report: ValidationReport, resource_id: str | None = None) -> set[str]:
    """Issue codes reported for one resource id (or for the whole report)."""
    return {i.code.value for i in report.issues if resource_id is None or i.resource_id == resource_id}


def good_patient(rid: str = "p1", **overrides) -> dict:
    return {
        "resourceType": "Patient",
        "id": rid,
        "identifier": [{"system": "urn:test:mrn", "value": f"MRN-{rid}"}],
        "name": [{"family": "Testwell", "given": ["Alex"]}],
        "gender": "female",
        "birthDate": "1980-05-12",
        **overrides,
    }


def good_condition(rid: str = "c1", **overrides) -> dict:
    return {
        "resourceType": "Condition",
        "id": rid,
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                       "code": "active"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "195967001"}]},
        "subject": {"reference": "Patient/p1"},
        "onsetDateTime": "2010-03-01",
        **overrides,
    }


def good_observation(rid: str = "o1", **overrides) -> dict:
    return {
        "resourceType": "Observation",
        "id": rid,
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
        "subject": {"reference": "Patient/p1"},
        "effectiveDateTime": "2023-01-01T10:00:00Z",
        "valueQuantity": {"value": 72, "unit": "/min"},
        **overrides,
    }


def without(resource: dict, *keys: str) -> dict:
    return {k: v for k, v in resource.items() if k not in keys}
