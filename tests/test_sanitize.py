from __future__ import annotations

import copy
import json

import pytest

from conftest import FIXED_NOW, good_condition, good_observation, good_patient, stored, without
from healthdata_insight.fhir import is_valid_id
from healthdata_insight.ingest import ingest_file
from healthdata_insight.models import IssueCode, PhiSeverity
from healthdata_insight.parser import parse_fhir_file
from healthdata_insight.phi import PSEUDONYM_SYSTEM
from healthdata_insight.samples import load_samples
from healthdata_insight.sanitize import (
    AddressAction,
    EventDateAction,
    IdentifierAction,
    PersonDateAction,
    SanitizationConfig,
    sanitize_dataset,
)
from healthdata_insight.validation import validate_dataset
from test_phi import RICH_PATIENT


def run(*resources, **config):
    return sanitize_dataset(stored(*resources), SanitizationConfig(**config), now=FIXED_NOW)


def sanitized_patient(**config) -> dict:
    return run(RICH_PATIENT, **config).resources[0]


def test_direct_identifiers_are_always_removed():
    patient = sanitized_patient()
    for key in ("name", "telecom", "photo", "contact", "text"):
        assert key not in patient
    assert patient.get("extension") == [{"url": "http://example.org/race", "valueString": "not flagged"}]


def test_clinically_useful_fields_are_preserved():
    observation = good_observation(text={"div": "<div>x</div>"})
    result = run(good_patient(), good_condition(), observation)
    patient, condition, sanitized_obs = result.resources
    assert patient["gender"] == "female"
    assert condition["code"] == good_condition()["code"]
    assert condition["clinicalStatus"] == good_condition()["clinicalStatus"]
    assert condition["onsetDateTime"] == "2010-03"  # event dates reduced to month by default
    assert sanitized_obs["valueQuantity"] == observation["valueQuantity"]
    assert sanitized_obs["status"] == "final"


def test_default_generalization():
    patient = sanitized_patient()
    assert patient["birthDate"] == "1980"
    assert patient["deceasedDateTime"] == "2020"
    assert patient["address"] == [{"state": "MA"}]
    [identifier] = patient["identifier"]
    assert identifier["system"] == PSEUDONYM_SYSTEM
    assert identifier["value"] != "MRN-p1"


@pytest.mark.parametrize("config, check", [
    ({"identifiers": IdentifierAction.REMOVE}, lambda p: "identifier" not in p),
    ({"addresses": AddressAction.REMOVE}, lambda p: "address" not in p),
    ({"person_dates": PersonDateAction.REMOVE}, lambda p: "birthDate" not in p and "deceasedDateTime" not in p),
    ({"person_dates": PersonDateAction.KEEP}, lambda p: p["birthDate"] == "1980-05-12"),
    ({"pseudonymize_ids": False}, lambda p: p["id"] == "p1"),
])
def test_configurable_options(config, check):
    assert check(sanitized_patient(**config))


def test_ages_over_89_are_grouped():
    old = good_patient(birthDate="1920-02-02")
    assert run(old).resources[0]["birthDate"] == "1936"  # FIXED_NOW is 2026
    assert run(old, group_ages_over_89=False).resources[0]["birthDate"] == "1920"


def test_unreadable_birth_date_is_removed_when_generalizing():
    assert "birthDate" not in run(good_patient(birthDate="1985-13-40")).resources[0]


class TestReferences:
    def test_ids_are_pseudonymized_and_references_follow(self):
        result = run(good_patient("p1"), good_condition(subject={"reference": "Patient/p1"}))
        patient, condition = result.resources
        assert patient["id"] != "p1" and is_valid_id(patient["id"])
        assert condition["subject"]["reference"] == f"Patient/{patient['id']}"
        assert result.references_preserved
        assert result.reference_issues_after == 0
        assert result.errors_after == 0  # only expected missing-field warnings (name was removed)

    def test_urn_uuid_references(self):
        result = run(good_patient("1111"), good_condition(subject={"reference": "urn:uuid:1111"}))
        patient, condition = result.resources
        assert condition["subject"]["reference"] == f"urn:uuid:{patient['id']}"
        assert not any(i.code is IssueCode.UNRESOLVED_REFERENCE for i in result.validation_after.issues)

    def test_absolute_reference_keeps_base_url(self):
        result = run(good_condition(subject={"reference": "https://fhir.example.org/Patient/p1/_history/3"}))
        ref = result.resources[0]["subject"]["reference"]
        assert ref.startswith("https://fhir.example.org/Patient/") and "p1" not in ref

    def test_reference_display_is_removed_but_coding_display_kept(self):
        condition = good_condition(subject={"reference": "Patient/p1", "display": "Alex Testwell"},
                                   code={"coding": [{"system": "http://snomed.info/sct", "code": "1",
                                                     "display": "Asthma"}]})
        sanitized = run(good_patient(), condition).resources[1]
        assert "display" not in sanitized["subject"]
        assert sanitized["code"]["coding"][0]["display"] == "Asthma"

    def test_unresolved_references_stay_unresolved_not_worse(self):
        result = run(good_patient(), good_condition(subject={"reference": "Patient/ghost"}))
        assert result.reference_issues_before == result.reference_issues_after == 1
        assert result.references_preserved

    def test_contained_and_malformed_references_are_left_alone(self):
        result = run(good_patient(), good_condition("c1", subject={"reference": "#contained"}),
                     good_condition("c2", subject={"reference": "not a reference"}))
        assert result.resources[1]["subject"]["reference"] == "#contained"
        assert result.resources[2]["subject"]["reference"] == "not a reference"


class TestPseudonyms:
    def test_random_key_by_default(self):
        assert run(good_patient()).resources[0]["id"] != run(good_patient()).resources[0]["id"]

    def test_secret_gives_stable_pseudonyms(self):
        first = run(good_patient(), secret="s3cret").resources[0]
        second = run(good_patient(), secret="s3cret").resources[0]
        other = run(good_patient(), secret="different").resources[0]
        assert first == second
        assert first["id"] != other["id"]

    def test_same_identifier_maps_to_same_pseudonym(self):
        shared = [{"system": "urn:mrn", "value": "MRN-9"}]
        result = run(good_patient("p1", identifier=shared), good_patient("p2", identifier=shared))
        assert result.resources[0]["identifier"] == result.resources[1]["identifier"]
        # ...so the probable-duplicate check still works on sanitized data.
        assert any(i.code is IssueCode.DUPLICATE_RECORD for i in result.validation_after.issues)

    def test_config_description_never_contains_the_secret(self):
        config = SanitizationConfig(secret="s3cret")
        assert "s3cret" not in json.dumps(config.describe())
        assert "s3cret" not in repr(config)
        assert config.describe()["stable_pseudonyms"] is True


class TestEventDates:
    def test_month(self):
        result = run(good_patient(), good_condition(abatementDateTime="2012-07-15", recordedDate="2010-03-02"),
                     good_observation(issued="2023-01-02T00:00:00Z"), event_dates=EventDateAction.MONTH)
        _, condition, observation = result.resources
        assert (condition["onsetDateTime"], condition["abatementDateTime"], condition["recordedDate"]) == (
            "2010-03", "2012-07", "2010-03")
        assert observation["effectiveDateTime"] == "2023-01"
        assert "issued" not in observation  # an instant can't be truncated
        assert not result.new_error_codes

    def test_year_with_period_and_instant(self):
        observation = without(good_observation(), "effectiveDateTime") | {
            "effectiveInstant": "2023-05-06T07:08:09Z"}
        condition = without(good_condition(), "onsetDateTime") | {
            "onsetPeriod": {"start": "2010-03-01", "end": "2011-04-01"}}
        result = run(good_patient(), condition, observation, event_dates=EventDateAction.YEAR)
        assert result.resources[1]["onsetPeriod"] == {"start": "2010", "end": "2011"}
        assert result.resources[2]["effectiveDateTime"] == "2023"
        assert "effectiveInstant" not in result.resources[2]
        assert not result.new_error_codes

    def test_dates_in_every_non_patient_resource_are_coarsened(self):
        encounter = {"resourceType": "Encounter", "id": "e1", "status": "finished",
                     "subject": {"reference": "Patient/p1"},
                     "period": {"start": "1980-05-12T08:00:00Z", "end": "1980-05-12T09:00:00Z"},
                     "meta": {"lastUpdated": "2024-01-01T00:00:00Z"}}
        claim = {"resourceType": "Claim", "id": "c1", "created": "1980-05-12T09:00:00Z",
                 "billablePeriod": {"start": "1980-05-12"},
                 "item": [{"servicedPeriod": {"start": "1980-05-12T08:00:00Z"}}]}
        result = run(good_patient(), encounter, claim, event_dates=EventDateAction.YEAR)
        patient, encounter_out, claim_out = result.resources
        assert patient["birthDate"] == "1980"  # Patient dates follow person_dates, not event_dates
        assert encounter_out["period"] == {"start": "1980", "end": "1980"}
        assert "lastUpdated" not in encounter_out["meta"]  # an instant can't be truncated
        assert claim_out["created"] == "1980"
        assert claim_out["billablePeriod"] == {"start": "1980"}
        assert claim_out["item"][0]["servicedPeriod"] == {"start": "1980"}
        assert "1980-05-12" not in result.to_ndjson().decode()

    def test_keep_leaves_event_dates_alone(self):
        encounter = {"resourceType": "Encounter", "id": "e1", "status": "finished",
                     "period": {"start": "1980-05-12T08:00:00Z"}}
        out = run(encounter, event_dates=EventDateAction.KEEP).resources[0]
        assert out["period"]["start"] == "1980-05-12T08:00:00Z"

    def test_codes_that_look_like_dates_are_untouched(self):
        observation = good_observation(code={"coding": [{"system": "s", "code": "2020-01-01"}]})
        out = run(good_patient(), observation, event_dates=EventDateAction.YEAR).resources[1]
        assert out["code"]["coding"][0]["code"] == "2020-01-01"  # 'code' is not a date element

    def test_non_date_onset_is_untouched(self):
        condition = without(good_condition(), "onsetDateTime") | {"onsetString": "childhood"}
        assert run(condition, event_dates=EventDateAction.YEAR).resources[0]["onsetString"] == "childhood"


def test_input_is_not_modified():
    original = copy.deepcopy(RICH_PATIENT)
    run(RICH_PATIENT)
    assert RICH_PATIENT == original


def test_actions_are_counted():
    actions = run(RICH_PATIENT).actions
    assert actions["Patient.name removed"] == 1
    assert actions["Patient.identifier pseudonymized"] == 1
    assert actions["narrative removed"] == 1
    assert actions["identifying extension removed"] == 2


def test_output_is_valid_ndjson_that_round_trips(store):
    result = run(good_patient(), good_condition(), good_observation())
    parsed = parse_fhir_file("sanitized.ndjson", result.to_ndjson())
    assert [r["resourceType"] for r in parsed.resources] == ["Patient", "Condition", "Observation"]


@pytest.mark.parametrize("sample_set", ["Clean synthetic data", "Corrupted synthetic data"])
@pytest.mark.parametrize("config", [
    SanitizationConfig(),
    SanitizationConfig(identifiers=IdentifierAction.REMOVE, addresses=AddressAction.REMOVE,
                       person_dates=PersonDateAction.REMOVE, event_dates=EventDateAction.YEAR),
])
def test_samples_sanitize_cleanly(store, sample_set, config):
    for name, data in load_samples(sample_set):
        ingest_file(store, name, data)
    before = validate_dataset(store.resources(), store.parse_issues(), now=FIXED_NOW)
    result = sanitize_dataset(store.resources(), config, validation_before=before, now=FIXED_NOW)

    assert result.residual_high_risk == 0
    assert result.residual.count(PhiSeverity.MEDIUM) == 0
    assert result.references_preserved
    assert result.new_error_codes == set()
    assert result.errors_after <= result.errors_before
    assert len(result.resources) == len(store.resources())

    # No original identifying value survives anywhere in the output.
    output = result.to_ndjson().decode("utf-8")
    for resource in store.resources():
        r = resource.resource
        if r["resourceType"] == "Patient":
            for name in r.get("name", []):
                assert name.get("family", "\0") not in output
            for telecom in r.get("telecom", []):
                assert telecom["value"] not in output
            for identifier in r.get("identifier", []):
                if identifier.get("value"):
                    assert f'"{identifier["value"]}"' not in output


def test_age_grouping_uses_age_at_death_for_deceased_patients():
    died_young = good_patient("p1", birthDate="1916-02-21", deceasedDateTime="1918-07-09T12:04:55-04:00")
    died_old = good_patient("p2", birthDate="1900-01-01", deceasedDateTime="1995-01-01T00:00:00Z",
                            identifier=[{"system": "s", "value": "v2"}])
    result = run(died_young, died_old)
    young, old = result.resources
    assert (young["birthDate"], young["deceasedDateTime"]) == ("1916", "1918")  # not grouped: died aged 2
    assert (old["birthDate"], old["deceasedDateTime"]) == ("1905", "1995")  # died aged 95: grouped at 90
    assert result.new_error_codes == set()
