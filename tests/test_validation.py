"""Validation tests built on deliberately corrupted FHIR data."""

from __future__ import annotations

import pytest

from conftest import (
    FIXED_NOW,
    codes,
    good_condition,
    good_observation,
    good_patient,
    validate,
    without,
)
from healthdata_insight.ingest import ingest_file
from healthdata_insight.models import IssueCode, Severity
from healthdata_insight.parser import parse_fhir_file
from healthdata_insight.samples import load_samples
from healthdata_insight.validation import validate_dataset


def test_clean_resources_have_no_issues():
    report = validate(good_patient(), good_condition(), good_observation())
    assert report.issues == ()
    assert report.valid_resource_count == 3
    assert report.invalid_record_count == 0


# --------------------------------------------------------------------------- structure

class TestStructure:
    def test_unknown_resource_type_is_an_error(self):
        report = validate({"resourceType": "Patinet", "id": "x"})
        assert codes(report) == {"unknown-resource-type"}
        assert report.invalid_record_count == 1

    def test_out_of_scope_types_are_reported_once_per_type(self):
        report = validate(*[{"resourceType": "Claim", "id": f"c{i}"} for i in range(500)],
                          {"resourceType": "Encounter", "id": "e1"}, good_patient())
        unsupported = [i for i in report.issues if i.code is IssueCode.UNSUPPORTED_RESOURCE_TYPE]
        assert [(i.resource_type, i.record_key) for i in unsupported] == [("Claim", None), ("Encounter", None)]
        assert "500 Claim" in unsupported[0].message
        assert report.valid_resource_count == 502

    def test_out_of_scope_type_is_only_a_warning(self):
        report = validate({"resourceType": "Encounter", "id": "e1"})
        assert codes(report) == {"unsupported-resource-type"}
        assert report.valid_resource_count == 1
        assert report.in_scope_count == 0

    @pytest.mark.parametrize("bad_id", ["has space", "a" * 65, 42])
    def test_invalid_id(self, bad_id):
        assert "invalid-id" in codes(validate(good_patient(bad_id)))

    def test_missing_id_is_a_warning(self):
        report = validate(without(good_patient(), "id"))
        assert codes(report) == {"missing-id"}
        assert report.valid_resource_count == 1

    @pytest.mark.parametrize("field, value", [
        ("name", "Alex Testwell"),          # must be a list
        ("name", ["Alex"]),                 # list entries must be objects
        ("identifier", {"value": "x"}),     # must be a list
        ("active", "yes"),                  # must be boolean
        ("deceasedBoolean", "false"),       # must be boolean
    ])
    def test_wrong_json_types_on_patient(self, field, value):
        report = validate(good_patient(**{field: value}))
        assert "invalid-structure" in codes(report)

    def test_multiple_choice_values(self):
        report = validate(good_patient(deceasedBoolean=True, deceasedDateTime="2020-01-01"))
        assert "invalid-structure" in codes(report)

    def test_reference_must_be_an_object(self):
        report = validate(good_patient(), good_condition(subject="Patient/p1"))
        assert codes(report, "c1") == {"invalid-structure"}

    def test_codeable_concept_must_be_an_object(self):
        report = validate(good_patient(), good_observation(code="8867-4"))
        assert "invalid-structure" in codes(report, "o1")


# --------------------------------------------------------------------------- Patient

class TestPatient:
    def test_missing_important_fields(self):
        report = validate({"resourceType": "Patient", "id": "p1"})
        missing = [i for i in report.issues if i.code is IssueCode.MISSING_FIELD]
        assert {i.path for i in missing} == {
            "Patient.identifier", "Patient.name", "Patient.gender", "Patient.birthDate"
        }
        assert all(i.severity is Severity.WARNING for i in missing)

    def test_empty_values_count_as_missing(self):
        report = validate(good_patient(name=[], gender="", identifier=None))
        assert sum(i.code is IssueCode.MISSING_FIELD for i in report.issues) == 3

    @pytest.mark.parametrize("birth_date", ["1985-13-40", "2021-02-29", "12/05/1980", "1980-05-12T00:00:00Z"])
    def test_invalid_birth_date(self, birth_date):
        report = validate(good_patient(birthDate=birth_date))
        assert codes(report) == {"invalid-date"}
        assert report.invalid_record_count == 1

    def test_future_birth_date_is_an_error(self):
        report = validate(good_patient(birthDate="2999-01-01"))
        [issue] = report.issues
        assert (issue.code, issue.severity) == (IssueCode.FUTURE_DATE, Severity.ERROR)

    def test_invalid_gender(self):
        assert codes(validate(good_patient(gender="M"))) == {"invalid-code"}

    def test_death_before_birth(self):
        report = validate(good_patient(deceasedDateTime="1970-01-01T00:00:00Z"))
        assert codes(report) == {"inconsistent-dates"}

    def test_empty_name_and_identifier_entries(self):
        report = validate(good_patient(name=[{"use": "official"}], identifier=[{"system": "urn:x"}]))
        assert codes(report) == {"invalid-value"}
        assert len(report.issues) == 2


# --------------------------------------------------------------------------- Condition

class TestCondition:
    def test_missing_subject_is_an_error(self):
        report = validate(without(good_condition(), "subject"))
        [issue] = report.issues
        assert (issue.code, issue.severity, issue.path) == (
            IssueCode.MISSING_REQUIRED_FIELD, Severity.ERROR, "Condition.subject"
        )

    @pytest.mark.parametrize("code", [None, {}, {"coding": []}, {"coding": [{"display": "Asthma"}]}])
    def test_missing_clinical_code_is_a_warning(self, code):
        condition = good_condition() if code is None else good_condition(code=code)
        if code is None:
            del condition["code"]
        report = validate(good_patient(), condition)
        missing = [i for i in report.issues if i.code is IssueCode.MISSING_CODE]
        assert len(missing) == 1 and missing[0].severity is Severity.WARNING

    def test_code_text_counts_as_a_code(self):
        assert validate(good_patient(), good_condition(code={"text": "Asthma"})).issues == ()

    def test_incomplete_coding(self):
        report = validate(good_patient(), good_condition(code={"coding": [{"code": "195967001"}]}))
        assert codes(report) == {"incomplete-coding"}

    def test_invalid_clinical_status(self):
        report = validate(good_patient(), good_condition(clinicalStatus={"coding": [{"code": "ongoing"}]}))
        assert "invalid-code" in codes(report)

    @pytest.mark.parametrize("reference", ["p1", "Patient/", "Patient/bad id"])
    def test_malformed_reference(self, reference):
        report = validate(good_patient(), good_condition(subject={"reference": reference}))
        assert codes(report, "c1") == {"invalid-reference"}

    def test_reference_to_wrong_type(self):
        report = validate(good_patient(), good_condition(subject={"reference": "Practitioner/p1"}))
        assert codes(report, "c1") == {"invalid-reference"}

    def test_empty_reference_object(self):
        report = validate(good_patient(), good_condition(subject={"type": "Patient"}))
        assert codes(report, "c1") == {"invalid-reference"}

    def test_unresolved_reference(self):
        report = validate(good_patient(), good_condition(subject={"reference": "Patient/ghost"}))
        [issue] = report.issues
        assert (issue.code, issue.severity) == (IssueCode.UNRESOLVED_REFERENCE, Severity.WARNING)

    def test_reference_not_checked_when_target_type_absent(self):
        # A Condition-only export can't be checked against Patients it doesn't contain.
        assert validate(good_condition(subject={"reference": "Patient/elsewhere"})).issues == ()

    def test_urn_uuid_and_logical_references(self):
        report = validate(
            good_patient("1111-2222"),
            good_condition("c1", subject={"reference": "urn:uuid:1111-2222"}),
            good_condition("c2", subject={"reference": "urn:uuid:9999"}),
            good_condition("c3", subject={"identifier": {"value": "MRN-1"}}),
        )
        assert codes(report, "c1") == set()
        assert codes(report, "c2") == {"unresolved-reference"}
        assert codes(report, "c3") == set()

    def test_abatement_before_onset_and_status_conflict(self):
        report = validate(good_patient(), good_condition(abatementDateTime="2005-01-01"))
        assert codes(report) == {"inconsistent-dates", "status-conflict"}

    def test_onset_before_birth(self):
        report = validate(good_patient(), good_condition(onsetDateTime="1970-06-01"))
        assert codes(report) == {"date-before-birth"}

    def test_same_year_onset_is_not_before_birth(self):
        # "1980" may be later than 1980-05-12, so it is not flagged.
        assert validate(good_patient(), good_condition(onsetDateTime="1980")).issues == ()

    def test_future_onset_is_a_warning(self):
        report = validate(good_patient(), good_condition(onsetDateTime="2030-01-01"))
        [issue] = report.issues
        assert (issue.code, issue.severity) == (IssueCode.FUTURE_DATE, Severity.WARNING)
        assert issue.record_key is not None and report.valid_resource_count == 2

    def test_period_ending_before_it_starts(self):
        report = validate(good_patient(), without(good_condition(), "onsetDateTime") | {
            "onsetPeriod": {"start": "2012-01-01", "end": "2011-01-01"}
        })
        assert codes(report) == {"inconsistent-dates"}


# --------------------------------------------------------------------------- Observation

class TestObservation:
    def test_completely_empty_observation(self):
        report = validate(good_patient(), {"resourceType": "Observation", "id": "o1"})
        by_path = {i.path: (i.code, i.severity) for i in report.issues}
        assert by_path == {
            "Observation.status": (IssueCode.MISSING_REQUIRED_FIELD, Severity.ERROR),
            "Observation.code": (IssueCode.MISSING_CODE, Severity.ERROR),
            "Observation.subject": (IssueCode.MISSING_FIELD, Severity.WARNING),
            "Observation.effective[x]": (IssueCode.MISSING_FIELD, Severity.WARNING),
            "Observation.value[x]": (IssueCode.MISSING_VALUE, Severity.ERROR),
        }

    def test_missing_value(self):
        report = validate(good_patient(), without(good_observation(), "valueQuantity"))
        assert codes(report) == {"missing-value"}
        assert report.invalid_record_count == 1

    @pytest.mark.parametrize("alternative", [
        {"dataAbsentReason": {"text": "Patient refused"}},
        {"component": [{"code": {"text": "Systolic"}, "valueQuantity": {"value": 120, "unit": "mm[Hg]"}}]},
        {"hasMember": [{"reference": "Observation/o9"}]},
    ])
    def test_value_alternatives(self, alternative):
        report = validate(good_patient(), without(good_observation(), "valueQuantity") | alternative)
        assert "missing-value" not in codes(report)

    def test_value_and_data_absent_reason(self):
        report = validate(good_patient(), good_observation(dataAbsentReason={"text": "n/a"}))
        assert codes(report) == {"invalid-structure"}

    def test_invalid_status(self):
        assert codes(validate(good_patient(), good_observation(status="done"))) == {"invalid-code"}

    @pytest.mark.parametrize("quantity, expected", [
        ({"value": "high", "unit": "/min"}, {"invalid-value"}),
        ({"value": True, "unit": "/min"}, {"invalid-value"}),
        ({"unit": "/min"}, {"invalid-value"}),
        ({"value": 72}, {"invalid-value"}),  # warning: no unit
        ("72 /min", {"invalid-structure"}),
    ])
    def test_bad_quantities(self, quantity, expected):
        assert codes(validate(good_patient(), good_observation(valueQuantity=quantity))) == expected

    def test_quantity_without_unit_is_only_a_warning(self):
        report = validate(good_patient(), good_observation(valueQuantity={"value": 72}))
        assert report.valid_resource_count == 2

    @pytest.mark.parametrize("key, value", [
        ("valueString", ""),
        ("valueInteger", 1.5),
        ("valueBoolean", "true"),
        ("valueCodeableConcept", {"coding": []}),
        ("valueDateTime", "2023-02-30"),
        ("valueBlob", "abc"),
    ])
    def test_bad_value_types(self, key, value):
        observation = without(good_observation(), "valueQuantity") | {key: value}
        report = validate(good_patient(), observation)
        assert report.invalid_record_count == 1, codes(report)

    def test_two_values(self):
        report = validate(good_patient(), good_observation(valueString="72"))
        assert "invalid-structure" in codes(report)

    def test_invalid_effective_date(self):
        assert codes(validate(good_patient(), good_observation(effectiveDateTime="yesterday"))) == {"invalid-date"}

    def test_effective_instant_requires_time(self):
        observation = without(good_observation(), "effectiveDateTime") | {"effectiveInstant": "2023-01-01"}
        assert codes(validate(good_patient(), observation)) == {"invalid-date"}

    def test_subject_may_be_a_device(self):
        report = validate(good_patient(), good_observation(subject={"reference": "Device/d1"}))
        assert report.issues == ()

    def test_component_without_code(self):
        report = validate(good_patient(), good_observation(component=[{"valueQuantity": {"value": 1, "unit": "x"}}]))
        assert codes(report) == {"invalid-structure"}

    def test_effective_before_birth(self):
        report = validate(good_patient(), good_observation(effectiveDateTime="1975-01-01T00:00:00Z"))
        assert codes(report) == {"date-before-birth"}


# --------------------------------------------------------------------------- duplicates

class TestDuplicates:
    def test_identical_copy(self):
        report = validate(good_patient(), good_patient())
        [issue] = report.issues
        assert (issue.code, issue.severity, issue.record_key) == (IssueCode.DUPLICATE_RECORD, Severity.WARNING, 2)

    def test_same_id_different_content(self):
        report = validate(good_patient(), good_patient(gender="male"))
        [issue] = report.issues
        assert (issue.code, issue.severity) == (IssueCode.CONFLICTING_DUPLICATE_ID, Severity.ERROR)

    def test_same_id_different_type_is_fine(self):
        assert validate(good_patient("x1"), good_observation("x1", subject={"reference": "Patient/x1"})).issues == ()

    def test_same_content_different_id(self):
        report = validate(good_patient(), good_observation("o1"), good_observation("o2"))
        assert [(i.code, i.resource_id) for i in report.issues] == [(IssueCode.DUPLICATE_RECORD, "o2")]

    def test_shared_patient_identifier(self):
        report = validate(good_patient("p1"), good_patient("p2", identifier=[{"system": "urn:test:mrn",
                                                                              "value": "MRN-p1"}]))
        [issue] = report.issues
        assert (issue.code, issue.resource_id) == (IssueCode.DUPLICATE_RECORD, "p2")

    def test_duplicates_without_ids(self):
        patient = without(good_patient(), "id")
        report = validate(patient, dict(patient))
        assert sum(i.code is IssueCode.DUPLICATE_RECORD for i in report.issues) == 1


# --------------------------------------------------------------------------- parse-level issues

class TestParseLevelIssues:
    def test_malformed_entries_are_coded_errors(self):
        data = b'{"resourceType":"Patient","id":"p1"}\n{broken\n{"id":"x"}\n42\n'
        parsed = parse_fhir_file("bad.ndjson", data)
        assert [(i.code, i.severity, i.line) for i in parsed.issues] == [
            (IssueCode.MALFORMED_JSON, Severity.ERROR, 2),
            (IssueCode.MISSING_RESOURCE_TYPE, Severity.ERROR, 3),
            (IssueCode.INVALID_STRUCTURE, Severity.ERROR, 4),
        ]

    def test_parse_issues_count_as_invalid_records(self, store):
        ingest_file(store, "bad.ndjson", b'{"resourceType":"Patient","id":"p1"}\n{broken\n{"id":"x"}\n')
        report = validate_dataset(store.resources(), store.parse_issues(), now=FIXED_NOW)
        assert report.total_records == 3
        assert report.malformed_record_count == 2
        assert report.invalid_record_count == 2
        assert report.valid_resource_count == 1

    def test_parse_warnings_do_not_invalidate_records(self, store):
        data = b'{"resourceType":"Bundle","entry":[{"resource":{"resourceType":"Patient"}},{"request":{}}]}'
        ingest_file(store, "b.json", data)
        report = validate_dataset(store.resources(), store.parse_issues(), now=FIXED_NOW)
        assert report.malformed_record_count == 0
        assert "invalid-bundle-entry" in codes(report)


# --------------------------------------------------------------------------- bundled samples

def _sample_report(store, sample_set):
    for name, data in load_samples(sample_set):
        ingest_file(store, name, data)
    return validate_dataset(store.resources(), store.parse_issues(), now=FIXED_NOW)


def test_clean_sample_is_valid(store):
    report = _sample_report(store, "Clean synthetic data")
    assert report.invalid_record_count == 0
    assert codes(report) == {"unsupported-resource-type"}  # the one Encounter


def test_corrupted_sample_triggers_every_annotated_issue(store):
    report = _sample_report(store, "Corrupted synthetic data")
    expected = {
        "pat-bad-date": {"invalid-date", "invalid-code", "missing-field"},
        "pat-sparse": {"missing-field"},
        "pat-future": {"future-date"},
        "pat-same-person": {"duplicate-record"},
        "bad id!": {"invalid-id"},
        "pat-dead-early": {"inconsistent-dates"},
        "cond-good": set(),
        "cond-no-subject": {"missing-required-field", "missing-code"},
        "cond-dangling": {"unresolved-reference", "inconsistent-dates", "status-conflict"},
        "cond-string-subject": {"invalid-structure"},
        "cond-before-birth": {"date-before-birth"},
        "obs-empty": {"missing-required-field", "missing-code", "missing-field", "missing-value"},
        "obs-bad-values": {"invalid-date", "invalid-value"},
        "obs-bad-status": {"invalid-code", "invalid-reference"},
        "obs-value-and-absent": {"invalid-structure"},
        "obs-copy": {"duplicate-record"},
        "obs-uncoded": {"missing-code", "incomplete-coding"},
        "enc-1": set(),  # out-of-scope types are reported once per type, not per resource
        "typo": {"unknown-resource-type"},
    }
    for resource_id, expected_codes in expected.items():
        assert codes(report, resource_id) == expected_codes, resource_id

    # pat-good and obs-good each appear twice: the copies are flagged, the originals are clean.
    assert [i.code.value for i in report.issues if i.resource_id == "pat-good"] == ["duplicate-record"]
    assert [i.code.value for i in report.issues if i.resource_id == "obs-good"] == ["conflicting-duplicate-id"]

    parse_codes = sorted(i.code.value for i in report.issues if i.line is not None)
    assert parse_codes == ["invalid-structure", "malformed-json", "missing-resource-type"]

    assert report.resource_count == 23
    assert report.malformed_record_count == 3
    assert report.valid_resource_count == 9
    assert report.invalid_record_count == 17


def test_messages_never_quote_patient_phi_values():
    report = validate(
        good_patient("p1", birthDate="1985-13-40"),
        good_patient("p2", birthDate="2999-01-01", identifier=[{"system": "urn:test:mrn", "value": "MRN-p1"}]),
        good_patient("p3", deceasedDateTime="not-a-date"),
    )
    messages = " ".join(i.message for i in report.issues)
    assert {"invalid-date", "future-date", "duplicate-record"} <= codes(report)
    for value in ("1985-13-40", "2999-01-01", "MRN-p1", "not-a-date"):
        assert value not in messages


def test_non_patient_messages_keep_values_for_debugging():
    report = validate(good_patient(), good_observation(effectiveDateTime="yesterday"))
    assert "yesterday" in report.issues[0].message
