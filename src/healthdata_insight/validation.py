"""FHIR R4 validation for Patient, Condition, and Observation resources.

This is a pragmatic data-quality validator, not a full conformance engine.
It checks the elements that matter for analysis:

* structure: known R4 resource type, JSON types/shapes, single choice[x] values
* required/important fields (see ``FIELD_SPECS``, which also drives completeness)
* code sets: Patient.gender, Condition clinical/verification status, Observation.status
* dates: real calendar dates, no future dates, sensible ordering
* references: well-formed, correct target type, resolvable within the dataset
* duplicates: repeated ids, identical content, shared Patient identifiers

Severity rules: ERROR means the record violates FHIR R4 or cannot be used
for analysis (it is counted as invalid); WARNING means it is usable but
incomplete or suspicious.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from healthdata_insight.config import IN_SCOPE_RESOURCE_TYPES
from healthdata_insight.fhir import (
    R4_RESOURCE_TYPES,
    DateRange,
    ParsedReference,
    is_valid_id,
    parse_fhir_datetime,
    parse_reference,
)
from healthdata_insight.models import (
    FieldCompleteness,
    IssueCode,
    Severity,
    ValidationIssue,
    ValidationReport,
)
from healthdata_insight.storage import StoredResource

ERROR, WARNING = Severity.ERROR, Severity.WARNING

PATIENT_GENDERS = frozenset({"male", "female", "other", "unknown"})
CONDITION_CLINICAL_STATUSES = frozenset({"active", "recurrence", "relapse", "inactive", "remission", "resolved"})
CONDITION_ACTIVE_STATUSES = frozenset({"active", "recurrence", "relapse"})
CONDITION_VERIFICATION_STATUSES = frozenset(
    {"unconfirmed", "provisional", "differential", "confirmed", "refuted", "entered-in-error"}
)
OBSERVATION_STATUSES = frozenset(
    {"registered", "preliminary", "final", "amended", "corrected", "cancelled", "entered-in-error", "unknown"}
)
OBSERVATION_VALUE_TYPES = frozenset({
    "valueQuantity", "valueCodeableConcept", "valueString", "valueBoolean", "valueInteger", "valueRange",
    "valueRatio", "valueSampledData", "valueTime", "valueDateTime", "valuePeriod",
})


# --------------------------------------------------------------------------- field specs

def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _choice_keys(resource: dict[str, Any], prefix: str) -> list[str]:
    """Keys of a FHIR choice element, e.g. ``onset`` -> ``onsetDateTime``, ``onsetPeriod``."""
    n = len(prefix)
    return [k for k in resource if k.startswith(prefix) and len(k) > n and k[n].isupper()]


def _codeable_has_code(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if isinstance(value.get("text"), str) and value["text"].strip():
        return True
    codings = value.get("coding")
    return isinstance(codings, list) and any(isinstance(c, dict) and c.get("code") for c in codings)


def _has(key: str) -> Callable[[dict], bool]:
    return lambda r: not _is_empty(r.get(key))


def _has_choice(prefix: str) -> Callable[[dict], bool]:
    return lambda r: any(not _is_empty(r[k]) for k in _choice_keys(r, prefix))


def _has_code(key: str) -> Callable[[dict], bool]:
    # A non-object value counts as present; the structure check reports it.
    return lambda r: not _is_empty(r.get(key)) and (not isinstance(r[key], dict) or _codeable_has_code(r[key]))


def _observation_has_value(r: dict) -> bool:
    return _has_choice("value")(r) or any(not _is_empty(r.get(k)) for k in ("dataAbsentReason", "component", "hasMember"))


@dataclass(frozen=True)
class FieldSpec:
    """An expected field: drives both missing-field issues and completeness scores."""

    field: str
    present: Callable[[dict], bool]
    code: IssueCode
    severity: Severity


_ID_SPEC = FieldSpec("id", _has("id"), IssueCode.MISSING_ID, WARNING)

FIELD_SPECS: dict[str, tuple[FieldSpec, ...]] = {
    "Patient": (
        _ID_SPEC,
        FieldSpec("identifier", _has("identifier"), IssueCode.MISSING_FIELD, WARNING),
        FieldSpec("name", _has("name"), IssueCode.MISSING_FIELD, WARNING),
        FieldSpec("gender", _has("gender"), IssueCode.MISSING_FIELD, WARNING),
        FieldSpec("birthDate", _has("birthDate"), IssueCode.MISSING_FIELD, WARNING),
    ),
    "Condition": (
        _ID_SPEC,
        FieldSpec("subject", _has("subject"), IssueCode.MISSING_REQUIRED_FIELD, ERROR),
        FieldSpec("code", _has_code("code"), IssueCode.MISSING_CODE, WARNING),
        FieldSpec("clinicalStatus", _has("clinicalStatus"), IssueCode.MISSING_FIELD, WARNING),
        FieldSpec("onset[x]", _has_choice("onset"), IssueCode.MISSING_FIELD, WARNING),
    ),
    "Observation": (
        _ID_SPEC,
        FieldSpec("status", _has("status"), IssueCode.MISSING_REQUIRED_FIELD, ERROR),
        FieldSpec("code", _has_code("code"), IssueCode.MISSING_CODE, ERROR),
        FieldSpec("subject", _has("subject"), IssueCode.MISSING_FIELD, WARNING),
        FieldSpec("effective[x]", _has_choice("effective"), IssueCode.MISSING_FIELD, WARNING),
        FieldSpec("value[x]", _observation_has_value, IssueCode.MISSING_VALUE, ERROR),
    ),
}
assert set(FIELD_SPECS) == set(IN_SCOPE_RESOURCE_TYPES)


# --------------------------------------------------------------------------- dataset index

@dataclass
class DatasetIndex:
    """What the dataset contains, for resolving references across resources."""

    keys: set[tuple[str, str]] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    birth_dates: dict[str, DateRange] = field(default_factory=dict)

    @classmethod
    def build(cls, resources: Sequence[StoredResource]) -> DatasetIndex:
        index = cls()
        for stored in resources:
            resource_type, rid = stored.resource["resourceType"], stored.resource.get("id")
            index.types.add(resource_type)
            if not isinstance(rid, str):
                continue
            index.keys.add((resource_type, rid))
            index.ids.add(rid)
            if resource_type == "Patient":
                try:
                    index.birth_dates[rid] = parse_fhir_datetime(stored.resource.get("birthDate"), date_only=True)
                except ValueError:
                    pass
        return index

    def resolves(self, ref: ParsedReference) -> bool | None:
        """True/False if resolvable; None if the dataset can't tell (external or type not loaded)."""
        if ref.absolute:
            return None
        if ref.resource_type is None:  # urn:uuid - Bundle fullUrls conventionally reuse the resource id
            return ref.resource_id in self.ids
        if ref.resource_type not in self.types:
            return None
        return (ref.resource_type, ref.resource_id) in self.keys


# --------------------------------------------------------------------------- per-resource checks

def _issue(stored: StoredResource, code: IssueCode, severity: Severity, message: str,
           path: str | None = None) -> ValidationIssue:
    rid = stored.resource.get("id")
    return ValidationIssue(
        code=code,
        severity=severity,
        message=message,
        resource_type=stored.resource["resourceType"],
        resource_id=rid if isinstance(rid, str) else None,
        path=path,
        file_name=stored.file_name,
        record_key=stored.key,
    )


class _ResourceChecker:
    def __init__(self, stored: StoredResource, index: DatasetIndex, now: datetime):
        self.stored = stored
        self.r = stored.resource
        self.type: str = self.r["resourceType"]
        self.index = index
        self.now = now
        self.issues: list[ValidationIssue] = []

    # -- helpers ---------------------------------------------------------------

    def add(self, code: IssueCode, severity: Severity, message: str, key: str | None = None) -> None:
        path = f"{self.type}.{key}" if key else None
        self.issues.append(_issue(self.stored, code, severity, message, path))

    def typed(self, key: str, kind: type | tuple[type, ...], kind_name: str, value: Any = None) -> Any:
        """Return the value if it has the expected JSON type, else report it and return None."""
        value = self.r.get(key) if value is None else value
        if value is None:
            return None
        if not isinstance(value, kind) or (isinstance(value, bool) and bool not in _as_tuple(kind)):
            self.add(IssueCode.INVALID_STRUCTURE, ERROR, f"{self.type}.{key} must be {kind_name}.", key)
            return None
        return value

    def objects(self, key: str) -> list[dict]:
        """A list-of-objects element (e.g. Patient.name). Reports bad shapes."""
        items = self.typed(key, list, "a list")
        if items is None:
            return []
        if not all(isinstance(item, dict) for item in items):
            self.add(IssueCode.INVALID_STRUCTURE, ERROR, f"Every {self.type}.{key} entry must be an object.", key)
        return [item for item in items if isinstance(item, dict)]

    def code(self, key: str, allowed: frozenset[str]) -> str | None:
        value = self.typed(key, str, "a string")
        if value is not None and value not in allowed:
            self.add(IssueCode.INVALID_CODE, ERROR,
                     f"{self.type}.{key} '{value}' is not one of: {', '.join(sorted(allowed))}.", key)
            return None
        return value

    def codeable(self, key: str, value: Any = None, allowed: frozenset[str] | None = None) -> str | None:
        """Check a CodeableConcept; return its first allowed code (when ``allowed`` is given)."""
        concept = self.typed(key, dict, "a CodeableConcept object", value)
        if concept is None:
            return None
        codings = concept.get("coding", [])
        if not isinstance(codings, list) or not all(isinstance(c, dict) for c in codings):
            self.add(IssueCode.INVALID_STRUCTURE, ERROR, f"{self.type}.{key}.coding must be a list of objects.", key)
            return None
        for coding in codings:
            if not coding.get("system") or not coding.get("code"):
                self.add(IssueCode.INCOMPLETE_CODING, WARNING,
                         f"A coding in {self.type}.{key} is missing its system or code.", key)
                break
        if allowed is None:
            return None
        codes = [c.get("code") for c in codings if c.get("code")]
        valid = [c for c in codes if c in allowed]
        if codes and not valid:
            self.add(IssueCode.INVALID_CODE, ERROR,
                     f"{self.type}.{key} '{codes[0]}' is not one of: {', '.join(sorted(allowed))}.", key)
        return valid[0] if valid else None

    def date(self, key: str, value: Any, *, date_only: bool = False, instant: bool = False,
             future: Severity = WARNING) -> DateRange | None:
        try:
            if instant and not (isinstance(value, str) and "T" in value):
                raise ValueError(f"'{value}' is not a full date and time with time zone")
            parsed = parse_fhir_datetime(value, date_only=date_only)
        except ValueError as exc:
            # Patient dates (birth, death) are PHI: describe the problem without quoting the value.
            detail = "is not a valid FHIR date" if self.type == "Patient" else str(exc)
            self.add(IssueCode.INVALID_DATE, ERROR, f"{self.type}.{key}: {detail}.", key)
            return None
        if parsed.start > self.now:
            shown = "" if self.type == "Patient" else f" '{value}'"
            self.add(IssueCode.FUTURE_DATE, future, f"{self.type}.{key}{shown} is in the future.", key)
        return parsed

    def period(self, key: str, value: Any) -> DateRange | None:
        period = self.typed(key, dict, "a Period object", value)
        if period is None:
            return None
        start = self.date(f"{key}.start", period["start"]) if "start" in period else None
        end = self.date(f"{key}.end", period["end"]) if "end" in period else None
        if start and end and end.definitely_before(start):
            self.add(IssueCode.INCONSISTENT_DATES, ERROR, f"{self.type}.{key} ends before it starts.", key)
        return start or end

    def choice_date(self, prefix: str) -> DateRange | None:
        """Validate a date-like choice element (onset[x], effective[x], ...); return its date."""
        keys = self.single_choice(prefix)
        for key in keys:
            value = self.r[key]
            if key == f"{prefix}DateTime":
                return self.date(key, value)
            if key == f"{prefix}Instant":
                return self.date(key, value, instant=True)
            if key == f"{prefix}Period":
                return self.period(key, value)
        return None

    def single_choice(self, prefix: str) -> list[str]:
        keys = _choice_keys(self.r, prefix)
        if len(keys) > 1:
            self.add(IssueCode.INVALID_STRUCTURE, ERROR,
                     f"Only one {prefix}[x] is allowed, found {', '.join(keys)}.", f"{prefix}[x]")
        return keys

    def reference(self, key: str, allowed_types: frozenset[str]) -> ParsedReference | None:
        value = self.r.get(key)
        if _is_empty(value):
            return None
        if not isinstance(value, dict):
            self.add(IssueCode.INVALID_STRUCTURE, ERROR, f"{self.type}.{key} must be a Reference object.", key)
            return None
        ref = value.get("reference")
        if ref is None:
            if not (value.get("identifier") or value.get("display")):  # FHIR ref-1
                self.add(IssueCode.INVALID_REFERENCE, ERROR,
                         f"{self.type}.{key} has no reference, identifier, or display.", key)
            return None
        if not isinstance(ref, str) or not ref:
            self.add(IssueCode.INVALID_STRUCTURE, ERROR, f"{self.type}.{key}.reference must be a string.", key)
            return None
        if ref.startswith("#"):  # contained resource
            return None
        parsed = parse_reference(ref)
        if parsed is None:
            self.add(IssueCode.INVALID_REFERENCE, ERROR, f"{self.type}.{key} '{ref}' is not a valid reference.", key)
            return None
        if parsed.resource_type is not None and parsed.resource_type not in allowed_types:
            self.add(IssueCode.INVALID_REFERENCE, ERROR,
                     f"{self.type}.{key} must reference {' or '.join(sorted(allowed_types))}, "
                     f"not {parsed.resource_type}.", key)
            return None
        if self.index.resolves(parsed) is False:
            self.add(IssueCode.UNRESOLVED_REFERENCE, WARNING,
                     f"{self.type}.{key} '{ref}' does not match any resource in the dataset.", key)
        return parsed

    def check_after_birth(self, key: str, when: DateRange | None, subject: ParsedReference | None) -> None:
        if when is None or subject is None or subject.resource_type not in (None, "Patient"):
            return
        birth = self.index.birth_dates.get(subject.resource_id)
        if birth is not None and when.definitely_before(birth):
            self.add(IssueCode.DATE_BEFORE_BIRTH, WARNING,
                     f"{self.type}.{key} is before the patient's birth date.", key)

    # -- entry point -------------------------------------------------------------

    def run(self, completeness: dict[tuple[str, str], list[int]]) -> None:
        if self.type not in R4_RESOURCE_TYPES:
            self.add(IssueCode.UNKNOWN_RESOURCE_TYPE, ERROR, f"'{self.type}' is not a FHIR R4 resource type.")
            return

        rid = self.r.get("id")
        if rid is not None and not is_valid_id(rid):
            self.add(IssueCode.INVALID_ID, ERROR,
                     f"Id {rid!r} is invalid (1-64 characters: letters, digits, '-' and '.').", "id")

        specs = FIELD_SPECS.get(self.type)
        if specs is None:
            return  # out of scope: reported once per type by validate_dataset, not once per resource

        for spec in specs:
            present = spec.present(self.r)
            counts = completeness[(self.type, spec.field)]
            counts[0] += 1
            counts[1] += present
            if not present:
                self.add(spec.code, spec.severity, f"{self.type}.{spec.field} is missing.", spec.field)

        {"Patient": self._patient, "Condition": self._condition, "Observation": self._observation}[self.type]()

    def _patient(self) -> None:
        for identifier in self.objects("identifier"):
            if _is_empty(identifier.get("value")):
                self.add(IssueCode.INVALID_VALUE, WARNING, "A Patient.identifier has no value.", "identifier")
        for name in self.objects("name"):
            if all(_is_empty(name.get(k)) for k in ("family", "given", "text")):
                self.add(IssueCode.INVALID_VALUE, WARNING, "A Patient.name has no family, given, or text.", "name")
        self.objects("telecom")
        self.objects("address")
        self.typed("active", bool, "true or false")
        self.code("gender", PATIENT_GENDERS)

        birth = None
        if "birthDate" in self.r:
            birth = self.date("birthDate", self.r["birthDate"], date_only=True, future=ERROR)

        self.single_choice("deceased")
        self.typed("deceasedBoolean", bool, "true or false")
        if "deceasedDateTime" in self.r:
            death = self.date("deceasedDateTime", self.r["deceasedDateTime"], future=ERROR)
            if birth and death and death.definitely_before(birth):
                self.add(IssueCode.INCONSISTENT_DATES, ERROR,
                         "Patient.deceasedDateTime is before Patient.birthDate.", "deceasedDateTime")

    def _condition(self) -> None:
        subject = self.reference("subject", frozenset({"Patient", "Group"}))
        self.codeable("code")
        self.objects("category")
        clinical = self.codeable("clinicalStatus", allowed=CONDITION_CLINICAL_STATUSES)
        verification = self.codeable("verificationStatus", allowed=CONDITION_VERIFICATION_STATUSES)

        onset = self.choice_date("onset")
        abatement = self.choice_date("abatement")
        if "recordedDate" in self.r:
            self.date("recordedDate", self.r["recordedDate"])
        if onset and abatement and abatement.definitely_before(onset):
            self.add(IssueCode.INCONSISTENT_DATES, ERROR, "Condition abatement is before its onset.", "abatement[x]")
        self.check_after_birth("onset[x]", onset, subject)

        # FHIR invariants con-4 and con-5.
        if _choice_keys(self.r, "abatement") and clinical in CONDITION_ACTIVE_STATUSES:
            self.add(IssueCode.STATUS_CONFLICT, WARNING,
                     f"Condition has an abatement date but clinicalStatus is '{clinical}'.", "clinicalStatus")
        if verification == "entered-in-error" and "clinicalStatus" in self.r:
            self.add(IssueCode.STATUS_CONFLICT, WARNING,
                     "clinicalStatus must be absent when verificationStatus is entered-in-error.", "clinicalStatus")

    def _observation(self) -> None:
        self.code("status", OBSERVATION_STATUSES)
        self.codeable("code")
        self.objects("category")
        subject = self.reference("subject", frozenset({"Patient", "Group", "Device", "Location"}))

        effective = self.choice_date("effective")
        if "issued" in self.r:
            self.date("issued", self.r["issued"], instant=True)
        self.check_after_birth("effective[x]", effective, subject)

        value_keys = self.single_choice("value")
        for key in value_keys:
            self._value(key, self.r[key])
        if value_keys and "dataAbsentReason" in self.r:  # FHIR obs-6
            self.add(IssueCode.INVALID_STRUCTURE, ERROR,
                     "Observation has both a value and a dataAbsentReason.", "dataAbsentReason")

        for i, component in enumerate(self.objects("component")):
            if not _codeable_has_code(component.get("code")):
                self.add(IssueCode.INVALID_STRUCTURE, ERROR, f"Observation.component[{i}] has no code.", "component")
            for key in _choice_keys(component, "value"):
                self._value(f"component[{i}].{key}", component[key], kind=key)

    def _value(self, key: str, value: Any, kind: str | None = None) -> None:
        kind = kind or key
        if kind not in OBSERVATION_VALUE_TYPES:
            self.add(IssueCode.INVALID_STRUCTURE, ERROR, f"{kind} is not a valid Observation value[x] type.", key)
        elif kind == "valueQuantity":
            quantity = self.typed(key, dict, "a Quantity object", value)
            if quantity is None:
                return
            number = quantity.get("value")
            if not isinstance(number, (int, float)) or isinstance(number, bool):
                self.add(IssueCode.INVALID_VALUE, ERROR,
                         f"Observation.{key}.value must be a number, got {number!r}.", key)
            if _is_empty(quantity.get("unit")) and _is_empty(quantity.get("code")):
                self.add(IssueCode.INVALID_VALUE, WARNING, f"Observation.{key} has no unit.", key)
        elif kind == "valueCodeableConcept":
            if self.typed(key, dict, "a CodeableConcept object", value) is not None:
                self.codeable(key, value)
                if not _codeable_has_code(value):
                    self.add(IssueCode.INVALID_VALUE, ERROR, f"Observation.{key} has no code or text.", key)
        elif kind == "valueString":
            if self.typed(key, str, "a string", value) is not None and not value.strip():
                self.add(IssueCode.INVALID_VALUE, ERROR, f"Observation.{key} is empty.", key)
        elif kind == "valueBoolean":
            self.typed(key, bool, "true or false", value)
        elif kind == "valueInteger":
            self.typed(key, int, "an integer", value)
        elif kind == "valueDateTime":
            self.date(key, value)
        elif kind == "valueTime":
            self.typed(key, str, "a time string", value)
        else:  # Range, Ratio, SampledData, Period
            self.typed(key, dict, "an object", value)


def _as_tuple(kind: type | tuple[type, ...]) -> tuple[type, ...]:
    return kind if isinstance(kind, tuple) else (kind,)


# --------------------------------------------------------------------------- duplicates

_CONTENT_EXCLUDED_KEYS = frozenset({"id", "meta", "text"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def find_duplicates(resources: Sequence[StoredResource]) -> list[ValidationIssue]:
    """Flag every resource after the first that repeats an id, content, or Patient identifier."""
    issues: list[ValidationIssue] = []
    by_id: dict[tuple[str, str], dict] = {}
    # Values are (storage key, label) of the first record seen.
    by_content: dict[tuple[str, bytes], tuple[int, str]] = {}
    by_identifier: dict[tuple[str, str], tuple[int, str]] = {}

    for stored in resources:
        r = stored.resource
        resource_type, rid = r["resourceType"], r.get("id")
        label = f"{resource_type}/{rid}" if isinstance(rid, str) else f"{resource_type} (no id)"

        if isinstance(rid, str):
            first = by_id.setdefault((resource_type, rid), r)
            if first is not r:
                if _canonical(first) == _canonical(r):
                    issues.append(_issue(stored, IssueCode.DUPLICATE_RECORD, WARNING, f"Identical copy of {label}."))
                else:
                    issues.append(_issue(stored, IssueCode.CONFLICTING_DUPLICATE_ID, ERROR,
                                         f"{label} is used by another record with different content."))
                continue

        # Without a name or identifier, matching demographics (e.g. in de-identified data) don't
        # imply the same person, so such Patients are not compared by content.
        anonymous_patient = resource_type == "Patient" and not (_has("name")(r) or _has("identifier")(r))
        if not anonymous_patient:
            # A digest, not the canonical string itself, so the index doesn't hold a second copy of the dataset.
            canonical = _canonical({k: v for k, v in r.items() if k not in _CONTENT_EXCLUDED_KEYS})
            content = (resource_type, hashlib.sha256(canonical.encode("utf-8")).digest())
            original_key, original = by_content.setdefault(content, (stored.key, label))
            if original_key != stored.key:
                issues.append(_issue(stored, IssueCode.DUPLICATE_RECORD, WARNING, f"Same content as {original}."))
                continue

        if resource_type == "Patient" and isinstance(r.get("identifier"), list):
            for identifier in r["identifier"]:
                if not isinstance(identifier, dict) or _is_empty(identifier.get("value")):
                    continue
                key = (str(identifier.get("system", "")), str(identifier["value"]))
                owner_key, owner = by_identifier.setdefault(key, (stored.key, label))
                if owner_key != stored.key:
                    issues.append(_issue(stored, IssueCode.DUPLICATE_RECORD, WARNING,
                                         f"Shares an identifier value with {owner}; likely the same person.",
                                         "Patient.identifier"))
                    break
    return issues


# --------------------------------------------------------------------------- entry point

def validate_dataset(
    resources: Sequence[StoredResource],
    parse_issues: Sequence[ValidationIssue] = (),
    *,
    now: datetime | None = None,
) -> ValidationReport:
    """Validate every resource in the dataset and roll results into a report."""
    now = now or datetime.now(timezone.utc)
    index = DatasetIndex.build(resources)
    completeness: dict[tuple[str, str], list[int]] = {
        (rt, spec.field): [0, 0] for rt, specs in FIELD_SPECS.items() for spec in specs
    }

    issues = list(parse_issues)
    in_scope = 0
    out_of_scope: dict[str, int] = {}
    for stored in resources:
        checker = _ResourceChecker(stored, index, now)
        checker.run(completeness)
        issues.extend(checker.issues)
        resource_type = stored.resource["resourceType"]
        if resource_type in FIELD_SPECS:
            in_scope += 1
        elif resource_type in R4_RESOURCE_TYPES:
            out_of_scope[resource_type] = out_of_scope.get(resource_type, 0) + 1
    # One dataset-level note per out-of-scope type: a warning per resource would bury real warnings
    # (a Synthea export has hundreds of thousands of Claims, Encounters, ...).
    issues.extend(
        ValidationIssue(IssueCode.UNSUPPORTED_RESOURCE_TYPE, WARNING,
                        f"{count:,} {resource_type} resource(s) are outside the analysis scope "
                        f"({', '.join(IN_SCOPE_RESOURCE_TYPES)}); they are counted but not analyzed.",
                        resource_type=resource_type)
        for resource_type, count in sorted(out_of_scope.items())
    )
    issues.extend(find_duplicates(resources))

    return ValidationReport(
        resource_count=len(resources),
        in_scope_count=in_scope,
        malformed_record_count=sum(1 for i in parse_issues if i.severity is ERROR),
        invalid_resource_keys=frozenset(
            i.record_key for i in issues if i.severity is ERROR and i.record_key is not None
        ),
        issues=tuple(issues),
        field_completeness=tuple(
            FieldCompleteness(rt, name, expected, present)
            for (rt, name), (expected, present) in completeness.items()
        ),
    )
