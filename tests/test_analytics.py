from __future__ import annotations

import pandas as pd
import pytest

from conftest import FIXED_NOW, good_condition, good_observation, good_patient, stored, validate, without
from healthdata_insight.analytics import (
    AGE_GROUP_ORDER,
    CohortFilter,
    age_distribution,
    age_group,
    build_analytical_dataset,
    condition_frequencies,
    gender_distribution,
    geography,
    observation_statistics,
    temporal_trend,
)
from healthdata_insight.ingest import ingest_file
from healthdata_insight.samples import load_samples
from healthdata_insight.validation import validate_dataset
from test_phi import RICH_PATIENT


def build(*resources):
    return build_analytical_dataset(stored(*resources), validate(*resources), now=FIXED_NOW)


def sample_dataset(store, sample_set="Clean synthetic data"):
    for name, data in load_samples(sample_set):
        ingest_file(store, name, data)
    report = validate_dataset(store.resources(), store.parse_issues(), now=FIXED_NOW)
    return build_analytical_dataset(store.resources(), report, now=FIXED_NOW)


def test_rows_and_linkage():
    data = build(good_patient("p1"), good_condition("c1"), good_observation("o1"))
    [patient] = data.patients.to_dict("records")
    [condition] = data.conditions.to_dict("records")
    [observation] = data.observations.to_dict("records")
    assert patient["patient_key"] != "p1"  # pseudonymous
    assert condition["patient_key"] == observation["patient_key"] == patient["patient_key"]
    assert (patient["gender"], patient["birth_year"], patient["age"], patient["age_group"]) == ("female", 1980, 46,
                                                                                              "35-49")
    assert condition["onset_month"] == "2010-03"
    assert (observation["value"], observation["unit"], observation["effective_month"]) == (72.0, "/min", "2023-01")


def test_keys_change_every_run():
    first, second = build(good_patient()), build(good_patient())
    assert first.patients["patient_key"][0] != second.patients["patient_key"][0]


def test_no_identifying_values_reach_the_analytical_dataset():
    rich = RICH_PATIENT | {"birthDate": "1980-05-12"}
    condition = good_condition(subject={"reference": "Patient/p1", "display": "Alex Testwell"},
                               code={"text": "Alex's asthma", "coding": [{"system": "s", "code": "1"}]},
                               note=[{"text": "Lives at 1 Example Street"}])
    observation = without(good_observation(), "valueQuantity") | {"valueString": "Alex reports MRN-p1"}
    data = build(rich, condition, observation)
    dumped = pd.concat([data.patients.astype(str), data.conditions.astype(str),
                        data.observations.astype(str)]).to_csv()
    for value in ("Testwell", "Alex", "555-0100", "alex@example.org", "Example Street", "Springfield", "01234",
                  "1980-05-12", "MRN-p1", "Maidenly", "Contactson", '"p1"', "2023-01-01T10:00:00Z"):
        assert value not in dumped, value
    assert data.observations["value_type"][0] == "text (not shown)"
    assert data.conditions["condition"][0] == "1"  # coding code, not the free-text label


def test_invalid_and_duplicate_records_are_excluded():
    data = build(good_patient("p1"), good_patient("p1"), good_patient("p2", gender="M"), good_observation("o1"))
    assert len(data.patients) == 1
    assert (data.excluded_invalid, data.excluded_duplicates) == (1, 1)


def test_ages_over_89_are_grouped_into_the_top_band():
    data = build(good_patient(birthDate="1920-01-01"))
    assert data.patients["age"][0] == 90
    assert data.patients["age_group"][0] == "80+"


@pytest.mark.parametrize("age, expected", [(0, "0-17"), (17, "0-17"), (18, "18-34"), (64, "50-64"), (80, "80+"),
                                           (None, None), (-1, None)])
def test_age_group(age, expected):
    assert age_group(age) == expected


def test_statistics_on_sample(store):
    data = sample_dataset(store)
    assert gender_distribution(data)["patients"].sum() == 18
    assert round(gender_distribution(data)["share_pct"].sum()) == 100
    ages = age_distribution(data)
    assert ages["patients"].sum() == 18 and list(ages["age_group"].cat.categories) == AGE_GROUP_ORDER
    assert geography(data)["patients"].sum() == 18

    conditions = condition_frequencies(data)
    assert conditions["patients"].is_monotonic_decreasing
    top = conditions.iloc[0]
    expected = data.conditions[data.conditions["condition"] == top["condition"]]["patient_key"].nunique()
    assert top["patients"] == expected and top["prevalence_pct"] == pytest.approx(100 * expected / 18)

    stats = observation_statistics(data)
    weight = stats[stats["code"] == "29463-7"].iloc[0]
    values = data.observations[data.observations["code"] == "29463-7"]["value"]
    assert weight["n"] == len(values) and weight["mean"] == pytest.approx(values.mean())
    assert weight["min"] <= weight["p25"] <= weight["median"] <= weight["p75"] <= weight["max"]


def test_cohort_filter(store):
    data = sample_dataset(store)
    filtered = CohortFilter(genders=("male",)).apply(data)
    assert set(filtered.patients["gender"]) == {"male"}
    keys = set(filtered.patients["patient_key"])
    assert set(filtered.conditions["patient_key"]) <= keys and set(filtered.observations["patient_key"]) <= keys
    assert CohortFilter().apply(data) is data


def test_temporal_trend_granularity():
    monthly = temporal_trend(pd.Series(["2023-01", "2023-01", "2023-03"]))
    assert list(monthly["count"]) == [2, 0, 1]  # gaps are filled with zero
    assert set(monthly["granularity"]) == {"month"}
    yearly = temporal_trend(pd.Series(["2010-05", "2020-01", "2020-07"]))
    assert set(yearly["granularity"]) == {"year"} and yearly["count"].sum() == 3 and len(yearly) == 11
    assert temporal_trend(pd.Series([], dtype=object)).empty


def test_empty_dataset():
    data = build({"resourceType": "Encounter", "id": "e1"})
    assert data.record_count == 0
    assert condition_frequencies(data).empty and observation_statistics(data).empty
    assert gender_distribution(data).empty


def test_corrupted_sample(store):
    data = sample_dataset(store, "Corrupted synthetic data")
    assert (len(data.patients), len(data.conditions), len(data.observations)) == (2, 2, 1)
    assert (data.excluded_invalid, data.excluded_duplicates) == (14, 3)


def test_age_is_age_at_death_for_deceased_patients():
    data = build(good_patient(birthDate="1940-06-01", deceasedDateTime="1990-03-01T00:00:00Z"))
    assert data.patients["age"][0] == 50 and data.patients["age_group"][0] == "50-64"
