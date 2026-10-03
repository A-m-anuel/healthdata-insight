"""Controlled defect injection for the validation, data-quality, and duplicate experiments.

Each defect is a deterministic mutation of a copy of one resource, paired with the issue code the
validator is expected to report on that resource. Ground truth therefore comes from construction,
not from the validator.
"""

from __future__ import annotations

import copy
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from healthdata_insight.models import IssueCode
from healthdata_insight.storage import StoredResource

Mutation = Callable[[dict], dict | None]  # returns the mutated copy, or None if not applicable


@dataclass(frozen=True)
class Defect:
    name: str
    resource_type: str
    expected: IssueCode
    description: str
    mutate: Mutation


def _with(r: dict, **changes: Any) -> dict:
    out = copy.deepcopy(r)
    out.update(changes)
    return out


def _without(r: dict, *keys: str) -> dict:
    out = copy.deepcopy(r)
    for key in keys:
        out.pop(key, None)
    return out


def _value_keys(r: dict) -> list[str]:
    return [k for k in r if k.startswith("value") and len(k) > 5 and k[5].isupper()]


def _set_effective(r: dict, value: str) -> dict | None:
    if "effectiveDateTime" not in r:
        return None
    return _with(r, effectiveDateTime=value)


def _subject_reference(r: dict, value: str) -> dict | None:
    if not isinstance(r.get("subject"), dict):
        return None
    out = copy.deepcopy(r)
    out["subject"]["reference"] = value
    return out


def _quantity_value(r: dict, value: Any) -> dict | None:
    if not isinstance(r.get("valueQuantity"), dict):
        return None
    out = copy.deepcopy(r)
    out["valueQuantity"]["value"] = value
    return out


def _drop_value(r: dict) -> dict | None:
    keys = _value_keys(r)
    if not keys or any(k in r for k in ("component", "hasMember", "dataAbsentReason")):
        return None
    return _without(r, *keys)


def _drop_choice(r: dict, prefix: str) -> dict | None:
    keys = [k for k in r if k.startswith(prefix) and len(k) > len(prefix) and k[len(prefix)].isupper()]
    return _without(r, *keys) if keys else None


def _status_conflict(r: dict) -> dict | None:
    if "abatementDateTime" not in r or not isinstance(r.get("clinicalStatus"), dict):
        return None
    out = copy.deepcopy(r)
    out["clinicalStatus"] = {"coding": [{
        "system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]}
    return out


def _requires(key: str, mutation: Mutation) -> Mutation:
    return lambda r: mutation(r) if key in r else None


# Structure and validity defects: each must make the resource invalid (an ERROR on that record).
VALIDATION_DEFECTS: tuple[Defect, ...] = (
    Defect("observation_status_not_in_code_set", "Observation", IssueCode.INVALID_CODE,
           "Observation.status = 'done'", lambda r: _with(r, status="done")),
    Defect("patient_gender_not_in_code_set", "Patient", IssueCode.INVALID_CODE,
           "Patient.gender = 'M'", lambda r: _with(r, gender="M")),
    Defect("observation_impossible_date", "Observation", IssueCode.INVALID_DATE,
           "effectiveDateTime = 2019-02-30T10:00:00Z", lambda r: _set_effective(r, "2019-02-30T10:00:00Z")),
    Defect("patient_birthdate_wrong_format", "Patient", IssueCode.INVALID_DATE,
           "birthDate in MM/DD/YYYY format", _requires("birthDate", lambda r: _with(r, birthDate="12/10/1945"))),
    Defect("condition_subject_not_an_object", "Condition", IssueCode.INVALID_STRUCTURE,
           "Condition.subject is a string", _requires("subject", lambda r: _with(r, subject="Patient/x"))),
    Defect("observation_code_not_an_object", "Observation", IssueCode.INVALID_STRUCTURE,
           "Observation.code is a string", _requires("code", lambda r: _with(r, code="8867-4"))),
    Defect("observation_value_not_numeric", "Observation", IssueCode.INVALID_VALUE,
           "valueQuantity.value = 'high'", lambda r: _quantity_value(r, "high")),
    Defect("observation_value_and_data_absent_reason", "Observation", IssueCode.INVALID_STRUCTURE,
           "value[x] plus dataAbsentReason (obs-6)",
           lambda r: _with(r, dataAbsentReason={"text": "unknown"}) if _value_keys(r) else None),
    Defect("observation_invalid_id", "Observation", IssueCode.INVALID_ID,
           "id contains a space (kept unique, so injected records don't collide)",
           lambda r: _with(r, id=f"{r['id']} invalid")),
    Defect("observation_unknown_resource_type", "Observation", IssueCode.UNKNOWN_RESOURCE_TYPE,
           "resourceType misspelled 'Observaton'", lambda r: _with(r, resourceType="Observaton")),
    Defect("observation_subject_wrong_type", "Observation", IssueCode.INVALID_REFERENCE,
           "subject references a Practitioner", lambda r: _subject_reference(r, "Practitioner/eval-1")),
    Defect("observation_status_missing", "Observation", IssueCode.MISSING_REQUIRED_FIELD,
           "Observation.status removed", _requires("status", lambda r: _without(r, "status"))),
    Defect("condition_subject_missing", "Condition", IssueCode.MISSING_REQUIRED_FIELD,
           "Condition.subject removed", _requires("subject", lambda r: _without(r, "subject"))),
    Defect("observation_code_missing", "Observation", IssueCode.MISSING_CODE,
           "Observation.code removed", _requires("code", lambda r: _without(r, "code"))),
    Defect("observation_value_missing", "Observation", IssueCode.MISSING_VALUE,
           "value[x] removed (no components or members)", _drop_value),
)

# Completeness and consistency defects (mostly warnings: the record stays usable).
QUALITY_DEFECTS: tuple[Defect, ...] = (
    Defect("patient_name_missing", "Patient", IssueCode.MISSING_FIELD, "Patient.name removed",
           _requires("name", lambda r: _without(r, "name"))),
    Defect("patient_gender_missing", "Patient", IssueCode.MISSING_FIELD, "Patient.gender removed",
           _requires("gender", lambda r: _without(r, "gender"))),
    Defect("patient_birthdate_missing", "Patient", IssueCode.MISSING_FIELD, "Patient.birthDate removed",
           _requires("birthDate", lambda r: _without(r, "birthDate"))),
    Defect("condition_code_missing", "Condition", IssueCode.MISSING_CODE, "Condition.code removed",
           _requires("code", lambda r: _without(r, "code"))),
    Defect("condition_onset_missing", "Condition", IssueCode.MISSING_FIELD, "onset[x] removed",
           lambda r: _drop_choice(r, "onset")),
    Defect("observation_effective_missing", "Observation", IssueCode.MISSING_FIELD, "effective[x] removed",
           lambda r: _drop_choice(r, "effective")),
    Defect("condition_onset_in_future", "Condition", IssueCode.FUTURE_DATE, "onsetDateTime = 2099-01-01",
           _requires("onsetDateTime", lambda r: _with(r, onsetDateTime="2099-01-01T00:00:00Z"))),
    Defect("condition_abatement_before_onset", "Condition", IssueCode.INCONSISTENT_DATES,
           "abatementDateTime = 1800-01-01",
           _requires("onsetDateTime", lambda r: _with(r, abatementDateTime="1800-01-01T00:00:00Z"))),
    Defect("observation_before_birth", "Observation", IssueCode.DATE_BEFORE_BIRTH,
           "effectiveDateTime = 1800-01-01", lambda r: _set_effective(r, "1800-01-01T00:00:00Z")),
    Defect("observation_unresolved_subject", "Observation", IssueCode.UNRESOLVED_REFERENCE,
           "subject points to a Patient not in the dataset",
           lambda r: _subject_reference(r, "Patient/eval-missing-patient")),
    Defect("condition_status_conflict", "Condition", IssueCode.STATUS_CONFLICT,
           "clinicalStatus 'active' on a condition with an abatement date", _status_conflict),
)

# Defects whose injection removes exactly one expected (completeness-scored) field.
COMPLETENESS_DEFECTS = frozenset({
    "patient_name_missing", "patient_gender_missing", "patient_birthdate_missing", "condition_code_missing",
    "condition_onset_missing", "observation_effective_missing",
})


@dataclass(frozen=True)
class Injection:
    key: int
    defect: Defect


def inject(
    resources: Sequence[StoredResource],
    defects: Sequence[Defect],
    per_defect: int,
    rng: random.Random,
) -> tuple[list[StoredResource], list[Injection]]:
    """Replace ``per_defect`` distinct resources per defect with mutated copies.

    Every resource receives at most one defect, so each detection can be attributed unambiguously.
    """
    by_type: dict[str, list[int]] = {}
    for index, stored in enumerate(resources):
        by_type.setdefault(stored.resource["resourceType"], []).append(index)
    for indices in by_type.values():
        rng.shuffle(indices)

    mutated = list(resources)
    used: set[int] = set()
    injections: list[Injection] = []
    for defect in defects:
        placed = 0
        for index in by_type.get(defect.resource_type, []):
            if placed == per_defect:
                break
            if index in used:
                continue
            changed = defect.mutate(resources[index].resource)
            if changed is None:
                continue
            used.add(index)
            mutated[index] = StoredResource(resources[index].key, resources[index].file_name, changed)
            injections.append(Injection(resources[index].key, defect))
            placed += 1
    return mutated, injections


# --------------------------------------------------------------------------- duplicates

@dataclass(frozen=True)
class DuplicateCase:
    name: str
    is_duplicate: bool  # ground truth: is the new record a duplicate of an existing one?
    expected: IssueCode | None
    description: str
    resource_type: str
    make: Callable[[dict, int], dict | None]  # (source resource, case number) -> new record


def _new_id(r: dict, n: int, **changes: Any) -> dict:
    return _with(r, id=f"eval-dup-{n}", **changes)


def _shift_quantity(r: dict, n: int) -> dict | None:
    if not isinstance(r.get("valueQuantity"), dict) or not isinstance(r["valueQuantity"].get("value"), (int, float)):
        return None
    out = _new_id(r, n)
    out["valueQuantity"]["value"] = r["valueQuantity"]["value"] + 1
    return out


def _shift_effective(r: dict, n: int) -> dict | None:
    value = r.get("effectiveDateTime")
    if not isinstance(value, str) or len(value) < 10:
        return None
    year = int(value[:4])
    return _new_id(r, n, effectiveDateTime=f"{year - 1:04d}{value[4:]}")


def _same_id_changed_value(r: dict, n: int) -> dict | None:
    changed = _shift_quantity(r, n)
    return _with(changed, id=r["id"]) if changed else None


def _patient_variant(r: dict, n: int, *, keep_identifiers: bool, keep_name: bool) -> dict | None:
    if not r.get("identifier") or not r.get("name"):
        return None
    out = _new_id(r, n)
    if not keep_identifiers:
        out["identifier"] = [{**i, "value": f"eval-{n}-{k}"} for k, i in enumerate(r["identifier"])]
    if not keep_name:
        out["name"] = [{"family": f"Evalname{n}", "given": [f"Given{n}"]}]
    return out


DUPLICATE_CASES: tuple[DuplicateCase, ...] = (
    DuplicateCase("exact_copy_same_id", True, IssueCode.DUPLICATE_RECORD,
                  "Identical copy of an Observation, same id", "Observation", lambda r, n: copy.deepcopy(r)),
    DuplicateCase("same_id_different_content", True, IssueCode.CONFLICTING_DUPLICATE_ID,
                  "Same Observation id, value changed", "Observation", _same_id_changed_value),
    DuplicateCase("same_content_new_id", True, IssueCode.DUPLICATE_RECORD,
                  "Observation copied under a new id", "Observation", lambda r, n: _new_id(r, n)),
    DuplicateCase("patient_shared_identifiers", True, IssueCode.DUPLICATE_RECORD,
                  "Patient re-registered: new id and name, same identifiers", "Patient",
                  lambda r, n: _patient_variant(r, n, keep_identifiers=True, keep_name=False)),
    DuplicateCase("patient_new_identifiers_same_demographics", True, None,
                  "Same person re-registered with new id and new identifiers (needs fuzzy matching)",
                  "Patient", lambda r, n: _patient_variant(r, n, keep_identifiers=False, keep_name=True)),
    DuplicateCase("observation_value_changed_new_id", False, None,
                  "New id and value + 1: a different measurement", "Observation", _shift_quantity),
    DuplicateCase("observation_date_changed_new_id", False, None,
                  "New id and effective date one year earlier: a different measurement", "Observation",
                  _shift_effective),
    DuplicateCase("patient_different_person", False, None,
                  "New id, new name, new identifiers (other demographics copied)", "Patient",
                  lambda r, n: _patient_variant(r, n, keep_identifiers=False, keep_name=False)),
)


@dataclass(frozen=True)
class DuplicateInjection:
    key: int
    case: DuplicateCase


def inject_duplicates(
    resources: Sequence[StoredResource],
    per_case: int,
    rng: random.Random,
) -> tuple[list[StoredResource], list[DuplicateInjection]]:
    """Append new records built from randomly chosen existing ones (sources are not modified)."""
    by_type: dict[str, list[StoredResource]] = {}
    for stored in resources:
        by_type.setdefault(stored.resource["resourceType"], []).append(stored)
    next_key = max(s.key for s in resources) + 1
    added: list[StoredResource] = []
    injections: list[DuplicateInjection] = []
    counter = 0
    for case in DUPLICATE_CASES:
        pool = list(by_type.get(case.resource_type, []))
        rng.shuffle(pool)
        placed = 0
        for source in pool:
            if placed == per_case:
                break
            counter += 1
            record = case.make(source.resource, counter)
            if record is None:
                continue
            added.append(StoredResource(next_key, source.file_name, record))
            injections.append(DuplicateInjection(next_key, case))
            next_key += 1
            placed += 1
    return list(resources) + added, injections
