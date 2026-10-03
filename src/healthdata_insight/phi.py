"""Rule-based detection of potential PHI in structured FHIR fields.

Every rule is a deterministic check on a known FHIR element: no machine
learning, NLP, or free-text pattern matching. This means the detector:

* finds identifying data in the fields it knows about (see ``RULES``), and
* does NOT find identifying data hidden in free text, non-standard
  extensions, or unexpected fields.

Findings record where potential PHI is, never the value itself. A finding
is a prompt for review, not a legal determination of PHI status, and this
tool makes no claim of HIPAA compliance.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from healthdata_insight.fhir import age_reference_year, parse_fhir_datetime, parse_reference
from healthdata_insight.models import PhiCategory, PhiFinding, PhiSeverity, PrivacyReport
from healthdata_insight.storage import StoredResource

HIGH, MEDIUM, LOW = PhiSeverity.HIGH, PhiSeverity.MEDIUM, PhiSeverity.LOW

BIRTH_PLACE_URL = "http://hl7.org/fhir/StructureDefinition/patient-birthPlace"
MOTHERS_MAIDEN_NAME_URL = "http://hl7.org/fhir/StructureDefinition/patient-mothersMaidenName"
# Identifier system used by the sanitizer for pseudonymized values.
PSEUDONYM_SYSTEM = "urn:healthdata-insight:pseudonym"

# Rule id -> (category, description). Severity can depend on the value's precision.
RULES: dict[str, tuple[PhiCategory, str]] = {
    "patient-name": (PhiCategory.NAME, "Patient.name with a family, given, or text part"),
    "patient-telecom": (PhiCategory.TELECOM, "Patient.telecom (phone, email, fax, ...)"),
    "patient-address-street": (PhiCategory.ADDRESS, "Patient.address line or full text"),
    "patient-address-city": (PhiCategory.ADDRESS, "Patient.address city or district"),
    "patient-address-postal-code": (PhiCategory.ADDRESS, "Patient.address postal code"),
    "patient-address-region": (PhiCategory.ADDRESS, "Patient.address state or country"),
    "patient-identifier": (PhiCategory.IDENTIFIER, "Patient.identifier value (MRN, SSN, ...)"),
    "patient-identifier-pseudonym": (PhiCategory.IDENTIFIER, "Pseudonymized Patient.identifier (a linkage key)"),
    "patient-id-matches-identifier": (PhiCategory.IDENTIFIER, "Patient.id equal to one of its identifier values"),
    "patient-birth-date": (PhiCategory.DATE, "Patient.birthDate (high-risk if day/month precision or age 90+)"),
    "patient-deceased-date": (PhiCategory.DATE, "Patient.deceasedDateTime"),
    "patient-photo": (PhiCategory.PHOTO, "Patient.photo"),
    "patient-contact": (PhiCategory.NAME, "Patient.contact name, telecom, or address (relatives, guardians)"),
    "patient-birth-place": (PhiCategory.ADDRESS, "patient-birthPlace extension"),
    "patient-mothers-maiden-name": (PhiCategory.NAME, "patient-mothersMaidenName extension"),
    "patient-reference-display": (PhiCategory.NAME, "display text on a reference to a Patient (often their name)"),
    "narrative": (PhiCategory.FREE_TEXT, "Narrative text.div (often repeats names and identifiers)"),
}

_CONTACT_CATEGORY = {"name": PhiCategory.NAME, "telecom": PhiCategory.TELECOM, "address": PhiCategory.ADDRESS}


def _filled(value: Any) -> bool:
    return value not in (None, "", [], {})


def _objects(value: Any) -> list[tuple[int, dict]]:
    return [(i, item) for i, item in enumerate(value) if isinstance(item, dict)] if isinstance(value, list) else []


class _Scanner:
    def __init__(self, stored: StoredResource, now: datetime):
        self.r = stored.resource
        self.type = self.r["resourceType"]
        rid = self.r.get("id")
        self.rid = rid if isinstance(rid, str) else None
        self.key = stored.key
        self.now = now
        self.findings: list[PhiFinding] = []

    def add(self, path: str, rule: str, severity: PhiSeverity, category: PhiCategory | None = None) -> None:
        self.findings.append(PhiFinding(
            resource_type=self.type,
            resource_id=self.rid,
            path=f"{self.type}.{path}",
            category=category or RULES[rule][0],
            severity=severity,
            rule=rule,
            record_key=self.key,
        ))

    def run(self) -> list[PhiFinding]:
        text = self.r.get("text")
        if isinstance(text, dict) and _filled(text.get("div")):
            self.add("text.div", "narrative", MEDIUM)
        for key, value in self.r.items():
            if isinstance(value, dict) and _filled(value.get("display")) and _targets_patient(value, key):
                self.add(f"{key}.display", "patient-reference-display", HIGH)
        if self.type == "Patient":
            self.patient()
        return self.findings

    def patient(self) -> None:
        r = self.r
        for i, name in _objects(r.get("name")):
            if any(_filled(name.get(k)) for k in ("family", "given", "text")):
                self.add(f"name[{i}]", "patient-name", HIGH)
        for i, telecom in _objects(r.get("telecom")):
            if _filled(telecom.get("value")):
                self.add(f"telecom[{i}]", "patient-telecom", HIGH)
        for i, address in _objects(r.get("address")):
            self.address(f"address[{i}]", address)

        identifier_values = set()
        for i, identifier in _objects(r.get("identifier")):
            if not _filled(identifier.get("value")):
                continue
            identifier_values.add(identifier["value"])
            if identifier.get("system") == PSEUDONYM_SYSTEM:
                self.add(f"identifier[{i}]", "patient-identifier-pseudonym", LOW)
            else:
                self.add(f"identifier[{i}]", "patient-identifier", HIGH)
        if self.rid is not None and self.rid in identifier_values:
            self.add("id", "patient-id-matches-identifier", HIGH)

        if _filled(r.get("birthDate")):
            self.add("birthDate", "patient-birth-date", self.date_severity(r["birthDate"], birth=True))
        if _filled(r.get("deceasedDateTime")):
            self.add("deceasedDateTime", "patient-deceased-date", self.date_severity(r["deceasedDateTime"]))
        for i, photo in _objects(r.get("photo")):
            self.add(f"photo[{i}]", "patient-photo", HIGH)

        for i, contact in _objects(r.get("contact")):
            for part, category in _CONTACT_CATEGORY.items():
                if _filled(contact.get(part)):
                    self.add(f"contact[{i}].{part}", "patient-contact", HIGH, category)

        for i, extension in _objects(r.get("extension")):
            if extension.get("url") == BIRTH_PLACE_URL:
                self.add(f"extension[{i}]", "patient-birth-place", MEDIUM)
            elif extension.get("url") == MOTHERS_MAIDEN_NAME_URL:
                self.add(f"extension[{i}]", "patient-mothers-maiden-name", HIGH)

    def address(self, path: str, address: dict) -> None:
        if _filled(address.get("line")) or _filled(address.get("text")):
            self.add(f"{path}.line", "patient-address-street", HIGH)
        if _filled(address.get("city")) or _filled(address.get("district")):
            self.add(f"{path}.city", "patient-address-city", MEDIUM)
        if _filled(address.get("postalCode")):
            self.add(f"{path}.postalCode", "patient-address-postal-code", MEDIUM)
        if _filled(address.get("state")) or _filled(address.get("country")):
            self.add(f"{path}.state", "patient-address-region", LOW)

    def date_severity(self, value: Any, *, birth: bool = False) -> PhiSeverity:
        """Day/month precision is a quasi-identifier; a bare year is low risk unless age is 90+."""
        try:
            parsed = parse_fhir_datetime(value)
        except ValueError:
            return MEDIUM  # unparseable: can't tell how precise it is, so be cautious
        if not (isinstance(value, str) and len(value) == 4):
            return MEDIUM
        if birth and age_reference_year(self.r, self.now) - parsed.start.year >= 90:
            return MEDIUM
        return LOW


def _targets_patient(reference: dict, key: str) -> bool:
    ref = reference.get("reference")
    if isinstance(ref, str):
        parsed = parse_reference(ref)
        if parsed is not None and parsed.resource_type is not None:
            return parsed.resource_type == "Patient"
    if reference.get("type") == "Patient":
        return True
    return key in ("subject", "patient")  # unresolvable (urn:uuid, logical): judge by the element name


def scan_resource(stored: StoredResource, now: datetime | None = None) -> list[PhiFinding]:
    return _Scanner(stored, now or datetime.now(timezone.utc)).run()


def detect_phi(resources: Sequence[StoredResource], *, now: datetime | None = None) -> PrivacyReport:
    now = now or datetime.now(timezone.utc)
    findings = [finding for stored in resources for finding in scan_resource(stored, now)]
    return PrivacyReport(tuple(findings), len(resources))
