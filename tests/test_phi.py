from __future__ import annotations

import json
from collections import Counter
from dataclasses import astuple

import pytest

from conftest import FIXED_NOW, good_condition, good_observation, good_patient, stored
from healthdata_insight.ingest import ingest_file
from healthdata_insight.models import DETECTION_METHOD, PhiCategory, PhiSeverity
from healthdata_insight.phi import PSEUDONYM_SYSTEM, RULES, detect_phi
from healthdata_insight.samples import load_samples


def scan(*resources):
    return detect_phi(stored(*resources), now=FIXED_NOW)


def by_path(report):
    return {f.path: (f.category, f.severity, f.rule) for f in report.findings}


RICH_PATIENT = good_patient(
    "p1",
    telecom=[{"system": "phone", "value": "555-0100"}, {"system": "email", "value": "alex@example.org"}],
    address=[{"line": ["1 Example Street"], "city": "Springfield", "postalCode": "01234", "state": "MA"}],
    photo=[{"contentType": "image/png", "data": "iVBORw0KGgo="}],
    contact=[{"name": {"family": "Contactson"}, "telecom": [{"system": "phone", "value": "555-0199"}]}],
    deceasedDateTime="2020-03-04T00:00:00Z",
    text={"status": "generated", "div": "<div>Alex Testwell</div>"},
    extension=[
        {"url": "http://hl7.org/fhir/StructureDefinition/patient-birthPlace", "valueAddress": {"city": "X"}},
        {"url": "http://hl7.org/fhir/StructureDefinition/patient-mothersMaidenName", "valueString": "Maidenly"},
        {"url": "http://example.org/race", "valueString": "not flagged"},
    ],
)


def test_every_patient_rule_fires_on_a_rich_patient():
    findings = by_path(scan(RICH_PATIENT))
    assert findings == {
        "Patient.text.div": (PhiCategory.FREE_TEXT, PhiSeverity.MEDIUM, "narrative"),
        "Patient.name[0]": (PhiCategory.NAME, PhiSeverity.HIGH, "patient-name"),
        "Patient.telecom[0]": (PhiCategory.TELECOM, PhiSeverity.HIGH, "patient-telecom"),
        "Patient.telecom[1]": (PhiCategory.TELECOM, PhiSeverity.HIGH, "patient-telecom"),
        "Patient.address[0].line": (PhiCategory.ADDRESS, PhiSeverity.HIGH, "patient-address-street"),
        "Patient.address[0].city": (PhiCategory.ADDRESS, PhiSeverity.MEDIUM, "patient-address-city"),
        "Patient.address[0].postalCode": (PhiCategory.ADDRESS, PhiSeverity.MEDIUM, "patient-address-postal-code"),
        "Patient.address[0].state": (PhiCategory.ADDRESS, PhiSeverity.LOW, "patient-address-region"),
        "Patient.identifier[0]": (PhiCategory.IDENTIFIER, PhiSeverity.HIGH, "patient-identifier"),
        "Patient.birthDate": (PhiCategory.DATE, PhiSeverity.MEDIUM, "patient-birth-date"),
        "Patient.deceasedDateTime": (PhiCategory.DATE, PhiSeverity.MEDIUM, "patient-deceased-date"),
        "Patient.photo[0]": (PhiCategory.PHOTO, PhiSeverity.HIGH, "patient-photo"),
        "Patient.contact[0].name": (PhiCategory.NAME, PhiSeverity.HIGH, "patient-contact"),
        "Patient.contact[0].telecom": (PhiCategory.TELECOM, PhiSeverity.HIGH, "patient-contact"),
        "Patient.extension[0]": (PhiCategory.ADDRESS, PhiSeverity.MEDIUM, "patient-birth-place"),
        "Patient.extension[1]": (PhiCategory.NAME, PhiSeverity.HIGH, "patient-mothers-maiden-name"),
    }


def test_findings_record_location_but_never_values():
    report = scan(RICH_PATIENT)
    finding = report.findings[0]
    assert (finding.resource_type, finding.resource_id, finding.method) == ("Patient", "p1", DETECTION_METHOD)
    dumped = json.dumps([astuple(f) for f in report.findings], default=str)
    for value in ("Testwell", "555-0100", "alex@example.org", "Example Street", "Springfield", "01234",
                  "1980-05-12", "MRN-p1", "Maidenly", "Contactson"):
        assert value not in dumped


def test_required_finding_fields_are_present():
    for finding in scan(RICH_PATIENT).findings:
        assert finding.resource_type and finding.path and finding.rule in RULES
        assert isinstance(finding.category, PhiCategory) and isinstance(finding.severity, PhiSeverity)
        assert finding.method == "fhir-field-rule"


def test_minimal_patient_has_no_findings():
    assert scan({"resourceType": "Patient", "id": "p1", "gender": "female"}).findings == ()


def test_empty_values_are_not_findings():
    report = scan({"resourceType": "Patient", "id": "p1", "name": [{"use": "official"}],
                   "telecom": [{"system": "phone"}], "identifier": [{"system": "x"}], "address": [{}]})
    assert report.findings == ()


@pytest.mark.parametrize("birth_date, severity", [
    ("1980-05-12", PhiSeverity.MEDIUM),
    ("1980-05", PhiSeverity.MEDIUM),
    ("1980", PhiSeverity.LOW),
    ("1930", PhiSeverity.MEDIUM),  # age 90+ is identifying even as a year
    ("not-a-date", PhiSeverity.MEDIUM),
])
def test_birth_date_severity_depends_on_precision(birth_date, severity):
    [finding] = [f for f in scan(good_patient(birthDate=birth_date)).findings if f.rule == "patient-birth-date"]
    assert finding.severity is severity


def test_id_that_equals_an_identifier_is_flagged():
    report = scan(good_patient("MRN-1", identifier=[{"system": "urn:mrn", "value": "MRN-1"}]))
    assert "patient-id-matches-identifier" in {f.rule for f in report.findings}


def test_pseudonymized_identifier_is_low_risk():
    report = scan(good_patient(identifier=[{"system": PSEUDONYM_SYSTEM, "value": "abc"}]))
    [finding] = [f for f in report.findings if f.category is PhiCategory.IDENTIFIER]
    assert (finding.rule, finding.severity) == ("patient-identifier-pseudonym", PhiSeverity.LOW)


@pytest.mark.parametrize("subject, flagged", [
    ({"reference": "Patient/p1", "display": "Alex Testwell"}, True),
    ({"reference": "urn:uuid:123", "display": "Alex Testwell"}, True),  # judged by element name
    ({"identifier": {"value": "MRN-1"}, "type": "Patient", "display": "Alex"}, True),
    ({"reference": "Group/g1", "display": "Diabetes cohort"}, False),
    ({"reference": "Patient/p1"}, False),
])
def test_reference_display_on_patient_references(subject, flagged):
    report = scan(good_condition(subject=subject))
    assert (("Condition.subject.display", "patient-reference-display") in
            {(f.path, f.rule) for f in report.findings}) is flagged


def test_practitioner_display_is_not_patient_phi():
    observation = good_observation(performer=[{"reference": "Practitioner/d1", "display": "Dr. Example"}])
    assert scan(observation).findings == ()


def test_narrative_on_any_resource():
    report = scan(good_observation(text={"status": "generated", "div": "<div>Alex: 72 bpm</div>"}))
    assert [f.path for f in report.findings] == ["Observation.text.div"]


def test_report_aggregates():
    report = scan(RICH_PATIENT, good_patient("p2"), good_condition(), {"resourceType": "Patient", "id": "p3"})
    assert report.resources_scanned == 4
    assert report.resources_affected == 2
    assert report.patients_affected == 2
    assert report.category_counts[PhiCategory.NAME] == 4  # p1 name, contact name, maiden name; p2 name
    assert sum(report.count(s) for s in PhiSeverity) == len(report.findings)


def test_clean_sample_findings(store):
    for name, data in load_samples():
        ingest_file(store, name, data)
    report = detect_phi(store.resources(), now=FIXED_NOW)
    rules = Counter(f.rule for f in report.findings)
    for rule in ("patient-name", "patient-telecom", "patient-address-street", "patient-identifier",
                 "patient-birth-date", "narrative"):
        assert rules[rule] == 18, rule
    assert rules["patient-contact"] > 0 and rules["patient-birth-place"] > 0
    assert report.patients_affected == 18
    assert {f.resource_type for f in report.findings} == {"Patient"}


def test_birth_year_severity_uses_age_at_death():
    child = scan(good_patient(birthDate="1916", deceasedDateTime="1918"))
    [finding] = [f for f in child.findings if f.rule == "patient-birth-date"]
    assert finding.severity is PhiSeverity.LOW  # died aged 2: a bare birth year is not an "age 90+" identifier
