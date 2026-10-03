"""Edge cases found uncovered during the engineering review (unusual or malformed input, failure paths)."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from conftest import good_observation, good_patient, stored, to_bytes, validate, without
from healthdata_insight import r_charts
from healthdata_insight.analytics import CohortFilter, build_analytical_dataset
from healthdata_insight.models import IssueCode
from healthdata_insight.parser import parse_fhir_file
from healthdata_insight.r_charts import RStatus, run_r_analysis
from healthdata_insight.reports import to_json

# --------------------------------------------------------------------------- validation


def codes_on(report, rid):
    return {i.code for i in report.issues if i.resource_id == rid}


@pytest.mark.parametrize("coding", ["8867-4", [{"code": "x"}, "not an object"]])
def test_coding_must_be_a_list_of_objects(coding):
    report = validate(good_patient(), good_observation(code={"coding": coding}))
    assert IssueCode.INVALID_STRUCTURE in codes_on(report, "o1")


@pytest.mark.parametrize("reference", [42, ""])
def test_reference_must_be_a_non_empty_string(reference):
    report = validate(good_patient(), good_observation(subject={"reference": reference}))
    assert IssueCode.INVALID_STRUCTURE in codes_on(report, "o1")


@pytest.mark.parametrize("key, value, valid", [
    ("valueTime", "10:30:00", True),
    ("valueTime", 1030, False),
    ("valueRange", {"low": {"value": 1}}, True),
    ("valueRange", "1-2", False),
    ("valueDateTime", "2023-01-01", True),
])
def test_other_value_types(key, value, valid):
    observation = without(good_observation(), "valueQuantity") | {key: value}
    report = validate(good_patient(), observation)
    assert (report.invalid_record_count == 0) is valid


def test_bundle_with_non_list_entry():
    data = to_bytes([{"resourceType": "Bundle", "entry": {"resource": {}}}, {"resourceType": "Patient"}])
    parsed = parse_fhir_file("b.json", data)
    assert len(parsed.resources) == 1
    assert [i.code for i in parsed.issues] == [IssueCode.INVALID_STRUCTURE]


# --------------------------------------------------------------------------- analytics

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def analytical(*resources):
    return build_analytical_dataset(stored(*resources), validate(*resources), now=NOW)


@pytest.mark.parametrize("value, expected", [
    ({"valueInteger": 3}, ("quantity", 3.0, None)),
    ({"valueCodeableConcept": {"coding": [{"system": "s", "code": "pos", "display": "Positive"}]}},
     ("coded", None, "Positive")),
    ({"valueBoolean": True}, ("coded", None, "true")),
    ({"valueString": "Patient says hello"}, ("text (not shown)", None, None)),
    ({"component": [{"code": {"text": "systolic"}, "valueQuantity": {"value": 120, "unit": "mm[Hg]"}}]},
     ("components", None, None)),
])
def test_observation_value_types(value, expected):
    data = analytical(good_patient(), without(good_observation(), "valueQuantity") | value)
    row = data.observations.iloc[0]
    actual_value = None if pd.isna(row["value"]) else row["value"]
    assert (row["value_type"], actual_value, row["value_label"]) == expected
    assert "Patient says hello" not in data.observations.to_csv()  # free text never extracted


def test_cohort_filter_by_age_group_and_state():
    data = analytical(
        good_patient("p1", birthDate="1990-01-01", address=[{"state": "MA"}]),
        good_patient("p2", birthDate="1950-01-01", address=[{"state": "CA"}],
                     identifier=[{"system": "s", "value": "other"}]),
    )
    assert len(CohortFilter(age_groups=("35-49",)).apply(data).patients) == 1
    assert list(CohortFilter(states=("CA",)).apply(data).patients["age_group"]) == ["65-79"]
    assert CohortFilter(states=("TX",)).apply(data).patients.empty


# --------------------------------------------------------------------------- R runner failure paths

def test_r_timeout_is_reported(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="Rscript", timeout=1)

    monkeypatch.setattr(r_charts.subprocess, "run", slow)
    output = run_r_analysis(analytical(good_patient()), rscript=r_charts.Path(sys.executable), timeout=1)
    assert output.status is RStatus.FAILED and "timed out" in output.message


def test_r_that_cannot_start_is_reported(tmp_path):
    output = run_r_analysis(analytical(good_patient()), rscript=tmp_path / "missing-Rscript.exe")
    assert output.status is RStatus.FAILED and "Could not start R" in output.message


def test_rscript_found_on_path(monkeypatch, tmp_path):
    monkeypatch.delenv(r_charts.RSCRIPT_ENV, raising=False)
    monkeypatch.setattr(r_charts.shutil, "which", lambda name: str(tmp_path / "Rscript"))
    assert r_charts.find_rscript() == tmp_path / "Rscript"


def test_rscript_not_found_anywhere(monkeypatch):
    monkeypatch.delenv(r_charts.RSCRIPT_ENV, raising=False)
    monkeypatch.setattr(r_charts.shutil, "which", lambda name: None)
    monkeypatch.setattr(r_charts.glob, "glob", lambda pattern: [])
    assert r_charts.find_rscript() is None


# --------------------------------------------------------------------------- report serialization

def test_report_json_handles_numpy_pandas_and_missing_values():
    report = {"enum": IssueCode.MISSING_VALUE, "when": datetime(2026, 1, 1, tzinfo=timezone.utc),
              "ts": pd.Timestamp("2026-01-01"), "i": np.int64(3), "f": np.float64(1.23456),
              "nan": math.nan, "inf": math.inf, "na": pd.NA}
    decoded = json.loads(to_json(report))
    assert decoded == {"enum": "missing-value", "when": "2026-01-01T00:00:00+00:00", "ts": "2026-01-01T00:00:00",
                       "i": 3, "f": 1.23456, "nan": None, "inf": None, "na": None}


def test_report_json_rejects_unknown_types():
    with pytest.raises(TypeError):
        to_json({"x": object()})
