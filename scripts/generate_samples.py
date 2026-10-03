"""Generate the bundled synthetic FHIR R4 sample files.

All people, identifiers, and clinical values are fictional. Output is
deterministic so the committed samples are reproducible:

* synthetic_bundle.json / synthetic_resources.ndjson: clean, randomly generated data
* synthetic_corrupted.ndjson: hand-written records with deliberate defects,
  each annotated below with the issues it should trigger

    python scripts/generate_samples.py
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "src" / "healthdata_insight" / "sample_data"

GIVEN = ["Alex", "Jordan", "Riley", "Casey", "Morgan", "Taylor", "Jamie", "Avery", "Quinn", "Rowan", "Sage", "Drew"]
FAMILY = ["Synthea", "Testwell", "Sampleton", "Mockford", "Demoson", "Fakely"]
CONDITIONS = [
    ("44054006", "Diabetes mellitus type 2"),
    ("38341003", "Hypertensive disorder"),
    ("195967001", "Asthma"),
    ("55822004", "Hyperlipidemia"),
    ("840539006", "COVID-19"),
]
OBSERVATIONS = [
    ("8302-2", "Body height", "cm", 150, 195),
    ("29463-7", "Body weight", "kg", 50, 120),
    ("8867-4", "Heart rate", "/min", 55, 100),
    ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%", 4.8, 9.5),
]


def patient(rng: random.Random, pid: str) -> dict:
    birth = date(1940, 1, 1) + timedelta(days=rng.randint(0, 365 * 65))
    family, given = rng.choice(FAMILY), rng.choice(GIVEN)
    resource = {
        "resourceType": "Patient",
        "id": pid,
        "meta": {"tag": [{"system": "urn:healthdata-insight", "code": "synthetic"}]},
        "identifier": [{"system": "urn:healthdata-insight:mrn", "value": f"SYN-{pid[-3:]}"}],
        "name": [{"family": family, "given": [given]}],
        "gender": rng.choice(["female", "male", "other", "unknown"]),
        "birthDate": birth.isoformat(),
        "address": [{"state": rng.choice(["MA", "CA", "TX", "NY", "WA"]), "country": "US"}],
    }
    add_contact_details(resource, f"{given} {family}")
    return resource


def add_contact_details(resource: dict, full_name: str) -> None:
    """Fictional PHI-like fields for privacy testing.

    Uses its own per-patient random generator, so the main sequence (and the
    resource counts the tests rely on) is unaffected. 555-01xx numbers and
    example.org addresses are reserved for fiction.
    """
    prng = random.Random(resource["id"])
    n = int(resource["id"][-3:])
    resource["telecom"] = [{"system": "phone", "value": f"555-01{prng.randint(0, 99):02d}", "use": "home"}]
    resource["address"][0] |= {
        "line": [f"{prng.randint(1, 999)} Example Street"],
        "city": prng.choice(["Springfield", "Riverton", "Fairview"]),
        "postalCode": f"{prng.randint(0, 99999):05d}",
    }
    resource["text"] = {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{full_name}</div>'}
    if n % 3 == 0:
        resource["extension"] = [{
            "url": "http://hl7.org/fhir/StructureDefinition/patient-birthPlace",
            "valueAddress": {"city": "Riverton", "state": resource["address"][0]["state"], "country": "US"},
        }]
    if n % 4 == 0:
        resource["contact"] = [{
            "relationship": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0131", "code": "N"}]}],
            "name": {"family": "Contactson", "given": ["Pat"]},
            "telecom": [{"system": "email", "value": f"contact{n}@example.org"}],
        }]


def condition(rng: random.Random, cid: str, pid: str) -> dict:
    code, display = rng.choice(CONDITIONS)
    onset = date(2015, 1, 1) + timedelta(days=rng.randint(0, 365 * 9))
    return {
        "resourceType": "Condition",
        "id": cid,
        "clinicalStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": code, "display": display}], "text": display},
        "subject": {"reference": f"Patient/{pid}"},
        "onsetDateTime": onset.isoformat(),
    }


def observation(rng: random.Random, oid: str, pid: str) -> dict:
    code, display, unit, low, high = rng.choice(OBSERVATIONS)
    value = round(rng.uniform(low, high), 1)
    effective = date(2022, 1, 1) + timedelta(days=rng.randint(0, 365 * 2))
    return {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": code, "display": display}], "text": display},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": effective.isoformat(),
        "valueQuantity": {"value": value, "unit": unit, "system": "http://unitsofmeasure.org", "code": unit},
    }


def resources_for(rng: random.Random, start: int, count: int) -> list[dict]:
    out: list[dict] = []
    for n in range(start, start + count):
        pid = f"syn-pat-{n:03d}"
        out.append(patient(rng, pid))
        out += [condition(rng, f"syn-cond-{n:03d}-{i}", pid) for i in range(rng.randint(0, 3))]
        out += [observation(rng, f"syn-obs-{n:03d}-{i}", pid) for i in range(rng.randint(2, 6))]
    return out


LOINC_HR = {"coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]}
SNOMED_ASTHMA = {"coding": [{"system": "http://snomed.info/sct", "code": "195967001", "display": "Asthma"}]}
ACTIVE = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]}
GOOD_PATIENT = {
    "resourceType": "Patient", "id": "pat-good",
    "identifier": [{"system": "urn:healthdata-insight:mrn", "value": "MRN-001"}],
    "name": [{"family": "Testwell", "given": ["Avery"]}], "gender": "female", "birthDate": "1980-05-12",
}
GOOD_OBSERVATION = {
    "resourceType": "Observation", "id": "obs-good", "status": "final", "code": LOINC_HR,
    "subject": {"reference": "Patient/pat-good"}, "effectiveDateTime": "2023-01-01T10:00:00Z",
    "valueQuantity": {"value": 72, "unit": "/min", "system": "http://unitsofmeasure.org", "code": "/min"},
}


def corrupted_records() -> list[dict | str]:
    """Records with deliberate defects. Strings are written verbatim (malformed lines)."""
    return [
        GOOD_PATIENT,  # valid
        {**GOOD_PATIENT, "id": "pat-bad-date", "identifier": [], "birthDate": "1985-13-40",
         "gender": "M"},  # invalid-date, invalid-code, missing-field (identifier)
        {"resourceType": "Patient", "id": "pat-sparse"},  # missing-field x4
        {**GOOD_PATIENT, "id": "pat-future", "identifier": [{"value": "MRN-002"}],
         "birthDate": "2999-01-01"},  # future-date (error)
        GOOD_PATIENT,  # duplicate-record (identical copy)
        {**GOOD_PATIENT, "id": "pat-same-person",
         "name": [{"family": "Testwell", "given": ["Ava"]}]},  # duplicate-record (shared identifier)
        {**GOOD_PATIENT, "id": "bad id!", "identifier": [{"value": "MRN-003"}]},  # invalid-id
        {**GOOD_PATIENT, "id": "pat-dead-early", "identifier": [{"value": "MRN-004"}], "birthDate": "1990-01-01",
         "deceasedDateTime": "1985-06-01T00:00:00Z"},  # inconsistent-dates
        {"resourceType": "Condition", "id": "cond-good", "clinicalStatus": ACTIVE, "code": SNOMED_ASTHMA,
         "subject": {"reference": "Patient/pat-good"}, "onsetDateTime": "2010-03-01"},  # valid
        {"resourceType": "Condition", "id": "cond-no-subject", "clinicalStatus": ACTIVE,
         "onsetDateTime": "2015-01-01"},  # missing-required-field (subject), missing-code
        {"resourceType": "Condition", "id": "cond-dangling", "clinicalStatus": ACTIVE, "code": SNOMED_ASTHMA,
         "subject": {"reference": "Patient/does-not-exist"}, "onsetDateTime": "2020-01-01",
         "abatementDateTime": "2019-01-01"},  # unresolved-reference, inconsistent-dates, status-conflict
        {"resourceType": "Condition", "id": "cond-string-subject", "clinicalStatus": ACTIVE, "code": SNOMED_ASTHMA,
         "subject": "Patient/pat-good", "onsetDateTime": "2012-01-01"},  # invalid-structure
        {"resourceType": "Condition", "id": "cond-before-birth", "clinicalStatus": ACTIVE, "code": SNOMED_ASTHMA,
         "subject": {"reference": "Patient/pat-good"}, "onsetDateTime": "1970-01-01"},  # date-before-birth
        GOOD_OBSERVATION,  # valid
        {"resourceType": "Observation", "id": "obs-empty"},  # missing status, code, subject, effective, value
        {**GOOD_OBSERVATION, "id": "obs-bad-values", "effectiveDateTime": "yesterday",
         "valueQuantity": {"value": "high", "unit": "/min"}},  # invalid-date, invalid-value
        {**GOOD_OBSERVATION, "id": "obs-bad-status", "status": "done",
         "subject": {"reference": "Practitioner/dr-1"}},  # invalid-code, invalid-reference
        {**GOOD_OBSERVATION, "id": "obs-value-and-absent",
         "dataAbsentReason": {"text": "Not measured"}},  # invalid-structure (obs-6)
        {**GOOD_OBSERVATION, "valueQuantity": {"value": 180, "unit": "/min"}},  # conflicting-duplicate-id
        {**GOOD_OBSERVATION, "id": "obs-copy"},  # duplicate-record (same content, new id)
        {**GOOD_OBSERVATION, "id": "obs-uncoded", "effectiveDateTime": "2023-02-01T10:00:00Z",
         "code": {"coding": [{"display": "Something"}]}},  # missing-code, incomplete-coding
        {"resourceType": "Encounter", "id": "enc-1", "status": "finished"},  # unsupported-resource-type
        {"resourceType": "Patinet", "id": "typo"},  # unknown-resource-type
        '{"resourceType": "Observation", "id": "broken"',  # malformed-json
        '{"id": "no-type", "status": "final"}',  # missing-resource-type
        '"just a string"',  # invalid-structure
    ]


def main() -> None:
    rng = random.Random(20240401)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    bundle = {
        "resourceType": "Bundle",
        "id": "synthetic-sample-bundle",
        "type": "collection",
        "entry": [
            {"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in resources_for(rng, 1, 12)
        ],
    }
    (OUT_DIR / "synthetic_bundle.json").write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")

    ndjson_resources = resources_for(rng, 13, 6)
    ndjson_resources.append({
        "resourceType": "Encounter",
        "id": "syn-enc-013-0",
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": {"reference": "Patient/syn-pat-013"},
    })
    lines = "\n".join(json.dumps(r, separators=(",", ":")) for r in ndjson_resources)
    (OUT_DIR / "synthetic_resources.ndjson").write_text(lines + "\n", encoding="utf-8")

    records = corrupted_records()
    lines = "\n".join(r if isinstance(r, str) else json.dumps(r, separators=(",", ":")) for r in records)
    (OUT_DIR / "synthetic_corrupted.ndjson").write_text(lines + "\n", encoding="utf-8")

    print(f"Wrote {len(bundle['entry'])} bundle entries, {len(ndjson_resources)} NDJSON lines, "
          f"and {len(records)} corrupted records to {OUT_DIR}")


if __name__ == "__main__":
    main()
