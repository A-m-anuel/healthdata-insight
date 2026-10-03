"""Configurable, privacy-preserving sanitization of a FHIR dataset.

What is always removed (direct identifiers with no analytic value):
Patient.name, telecom, photo, contact, the birthPlace / mothersMaidenName
extensions, narrative ``text`` on every resource, and ``display`` text on
every Reference.

What is configurable (see :class:`SanitizationConfig`):
identifiers, addresses, birth/death dates, clinical event dates (every
date in every resource other than Patient: encounters, claims,
observations, ...), and whether resource ids are replaced with pseudonyms.

What is preserved: resource types, codes, values, statuses, gender, and
(depending on configuration) birth year, state/country, and event dates.
References are rewritten consistently with any new ids, so they still
resolve within the sanitized dataset.

Pseudonyms are keyed HMAC-SHA256 values. The key is random for each run
unless the caller supplies a secret, and it is never stored, so pseudonyms
cannot be reversed from the output. A shared secret makes pseudonyms stable
across runs, which allows linking datasets; keep it confidential.

Sanitization reduces re-identification risk. It does not guarantee
de-identification, and this tool makes no claim of HIPAA compliance.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from healthdata_insight.fhir import age_reference_year, parse_fhir_datetime, parse_reference
from healthdata_insight.models import IssueCode, PhiSeverity, PrivacyReport, Severity, ValidationReport
from healthdata_insight.phi import BIRTH_PLACE_URL, MOTHERS_MAIDEN_NAME_URL, PSEUDONYM_SYSTEM, detect_phi
from healthdata_insight.storage import StoredResource
from healthdata_insight.validation import validate_dataset


class IdentifierAction(str, Enum):
    PSEUDONYMIZE = "pseudonymize"
    REMOVE = "remove"


class AddressAction(str, Enum):
    GENERALIZE = "generalize"  # keep only state and country
    REMOVE = "remove"


class PersonDateAction(str, Enum):
    YEAR = "year"
    REMOVE = "remove"
    KEEP = "keep"


class EventDateAction(str, Enum):
    KEEP = "keep"
    MONTH = "month"
    YEAR = "year"


@dataclass(frozen=True)
class SanitizationConfig:
    identifiers: IdentifierAction = IdentifierAction.PSEUDONYMIZE
    addresses: AddressAction = AddressAction.GENERALIZE
    person_dates: PersonDateAction = PersonDateAction.YEAR  # Patient.birthDate / deceasedDateTime
    group_ages_over_89: bool = True  # with YEAR: birth years 90+ years ago become (current year - 90)
    # Dates in all non-Patient resources. Month by default: exact event dates can reveal the birth date
    # (newborn encounters, immunizations, and observations are dated on the day of birth).
    event_dates: EventDateAction = EventDateAction.MONTH
    pseudonymize_ids: bool = True
    secret: str | None = field(default=None, repr=False)

    def describe(self) -> dict[str, Any]:
        """Settings safe to log or store: the secret itself is never included."""
        settings = {k: (v.value if isinstance(v, Enum) else v) for k, v in asdict(self).items() if k != "secret"}
        settings["stable_pseudonyms"] = self.secret is not None
        return settings


# Elements holding dates or times in FHIR R4 resources (besides any ``...DateTime``/``...Date`` element).
_DATE_ELEMENTS = frozenset({"date", "start", "end", "created", "authoredOn", "issued", "recorded", "lastUpdated",
                            "sent", "received", "whenPrepared", "whenHandedOver", "occurrenceDateTime"})
# FHIR ``instant`` elements must keep a full timestamp, so they are removed rather than truncated.
_INSTANT_ELEMENTS = frozenset({"issued", "recorded", "lastUpdated"})
_UUID_FROM_HEX = re.compile(r"(.{8})(.{4})(.{4})(.{4})(.{12})")


class _Sanitizer:
    def __init__(self, config: SanitizationConfig, now: datetime):
        self.config = config
        self.now = now
        self.key = config.secret.encode("utf-8") if config.secret else secrets.token_bytes(32)
        self.actions: Counter[str] = Counter()

    def pseudonym(self, value: str, namespace: str) -> str:
        digest = hmac.new(self.key, f"{namespace}|{value}".encode("utf-8"), hashlib.sha256).hexdigest()
        return "-".join(_UUID_FROM_HEX.match(digest[:32]).groups())  # UUID-shaped: valid FHIR id and urn:uuid

    def remove(self, container: dict, key: str, action: str) -> None:
        if container.pop(key, None) not in (None, "", [], {}):
            self.actions[action] += 1

    # -- resources ------------------------------------------------------------

    def resource(self, original: dict) -> dict:
        r = copy.deepcopy(original)
        self.remove(r, "text", "narrative removed")
        if self.config.pseudonymize_ids and isinstance(r.get("id"), str):
            r["id"] = self.pseudonym(r["id"], "id")
            self.actions["resource id pseudonymized"] += 1
        self.references(r)
        if r["resourceType"] == "Patient":
            self.patient(r)
        if self.config.event_dates is not EventDateAction.KEEP and r["resourceType"] != "Patient":
            self.event_dates(r)  # Patient dates follow ``person_dates``
        return r

    def references(self, value: Any) -> None:
        """Rewrite every Reference in the resource; drop reference display text."""
        if isinstance(value, list):
            for item in value:
                self.references(item)
            return
        if not isinstance(value, dict):
            return
        if isinstance(value.get("reference"), str):
            self.remove(value, "display", "reference display removed")
            if self.config.pseudonymize_ids:
                value["reference"] = self.rewrite_reference(value["reference"])
        for child in value.values():
            self.references(child)

    def rewrite_reference(self, ref: str) -> str:
        if ref.startswith("#"):
            return ref  # contained resource; ids inside a resource are not pseudonymized
        parsed = parse_reference(ref)
        if parsed is None:
            return ref  # malformed; validation reports it
        new_id = self.pseudonym(parsed.resource_id, "id")
        self.actions["reference rewritten"] += 1
        if parsed.resource_type is None:
            return f"{ref.split(':', 2)[0]}:{ref.split(':', 2)[1]}:{new_id}"
        prefix = ref[: ref.find(f"{parsed.resource_type}/{parsed.resource_id}")] if parsed.absolute else ""
        return f"{prefix}{parsed.resource_type}/{new_id}"

    def patient(self, r: dict) -> None:
        for key in ("name", "telecom", "photo", "contact"):
            self.remove(r, key, f"Patient.{key} removed")

        if isinstance(r.get("extension"), list):
            kept = [e for e in r["extension"]
                    if not (isinstance(e, dict) and e.get("url") in (BIRTH_PLACE_URL, MOTHERS_MAIDEN_NAME_URL))]
            self.actions["identifying extension removed"] += len(r["extension"]) - len(kept)
            if kept:
                r["extension"] = kept
            else:
                del r["extension"]

        if self.config.identifiers is IdentifierAction.REMOVE:
            self.remove(r, "identifier", "Patient.identifier removed")
        elif isinstance(r.get("identifier"), list):
            r["identifier"] = [self.identifier(i) for i in r["identifier"] if isinstance(i, dict)]

        if self.config.addresses is AddressAction.REMOVE:
            self.remove(r, "address", "Patient.address removed")
        elif isinstance(r.get("address"), list):
            generalized = [
                {k: a[k] for k in ("use", "type", "state", "country") if a.get(k)}
                for a in r["address"] if isinstance(a, dict)
            ]
            generalized = [a for a in generalized if "state" in a or "country" in a]
            self.actions["Patient.address generalized"] += len(r["address"])
            if generalized:
                r["address"] = generalized
            else:
                del r["address"]

        self.person_date(r, "birthDate", birth=True)
        self.person_date(r, "deceasedDateTime")

    def identifier(self, identifier: dict) -> dict:
        """Replace the value with a pseudonym. The original system is dropped (the new value isn't
        valid there), but ``type`` is kept so the kind of identifier (MRN, SSN, ...) stays known."""
        kept = {k: identifier[k] for k in ("use", "type") if k in identifier}
        value = identifier.get("value")
        if isinstance(value, str) and value:
            kept["system"] = PSEUDONYM_SYSTEM
            kept["value"] = self.pseudonym(f"{identifier.get('system', '')}|{value}", "identifier")
            self.actions["Patient.identifier pseudonymized"] += 1
        return kept

    def person_date(self, r: dict, key: str, *, birth: bool = False) -> None:
        if key not in r or self.config.person_dates is PersonDateAction.KEEP:
            return
        if self.config.person_dates is PersonDateAction.REMOVE:
            self.remove(r, key, f"Patient.{key} removed")
            return
        try:
            year = parse_fhir_datetime(r[key]).start.year
        except ValueError:
            self.remove(r, key, f"Patient.{key} removed (unreadable)")
            return
        # Age at death for deceased patients (birthDate is processed before deceasedDateTime changes),
        # so grouping never moves a birth year past the year of death.
        reference = age_reference_year(r, self.now) if birth else self.now.year
        if birth and self.config.group_ages_over_89 and reference - year >= 90:
            year = reference - 90
            self.actions["age 90+ grouped"] += 1
        r[key] = f"{year:04d}"
        self.actions[f"Patient.{key} reduced to year"] += 1

    def event_dates(self, value: Any) -> None:
        """Coarsen every date and time in a (non-Patient) resource, at any depth."""
        if isinstance(value, list):
            for item in value:
                self.event_dates(item)
            return
        if not isinstance(value, dict):
            return
        length = 7 if self.config.event_dates is EventDateAction.MONTH else 4
        for key in list(value):
            item = value[key]
            if isinstance(item, (dict, list)):
                self.event_dates(item)
            elif isinstance(item, str) and _is_date_element(key) and _is_fhir_date(item):
                if key.endswith("Instant"):  # choice element: becomes the dateTime variant
                    del value[key]
                    value[f"{key[: -len('Instant')]}DateTime"] = item[:length]
                elif key in _INSTANT_ELEMENTS:
                    del value[key]
                else:
                    value[key] = item[:length]
                self.actions["event date generalized"] += 1



def _is_date_element(key: str) -> bool:
    return key in _DATE_ELEMENTS or key.endswith(("DateTime", "Date", "Instant"))


def _is_fhir_date(value: str) -> bool:
    """Only well-formed dates are coarsened; anything else (e.g. ``onsetString``) is left for validation."""
    try:
        parse_fhir_datetime(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class SanitizationResult:
    config: SanitizationConfig
    resources: tuple[dict, ...]
    actions: dict[str, int]
    validation_before: ValidationReport
    validation_after: ValidationReport
    residual: PrivacyReport  # PHI scan of the sanitized output
    created_at: datetime

    @property
    def residual_high_risk(self) -> int:
        return self.residual.count(PhiSeverity.HIGH)

    @property
    def reference_issues_before(self) -> int:
        return _reference_issue_count(self.validation_before)

    @property
    def reference_issues_after(self) -> int:
        return _reference_issue_count(self.validation_after)

    @property
    def errors_before(self) -> int:
        """Resource-level errors in the input (malformed lines can't reach the output, so they're excluded)."""
        return sum(i.severity is Severity.ERROR and i.record_key is not None for i in self.validation_before.issues)

    @property
    def errors_after(self) -> int:
        return self.validation_after.error_count

    @property
    def references_preserved(self) -> bool:
        return self.reference_issues_after <= self.reference_issues_before

    @property
    def new_error_codes(self) -> set[str]:
        """Error codes present after sanitization that were not present before."""
        return _error_codes(self.validation_after) - _error_codes(self.validation_before)

    def to_ndjson(self) -> bytes:
        return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in self.resources).encode("utf-8")


def _reference_issue_count(report: ValidationReport) -> int:
    return sum(i.code in (IssueCode.UNRESOLVED_REFERENCE, IssueCode.INVALID_REFERENCE) for i in report.issues)


def _error_codes(report: ValidationReport) -> set[str]:
    return {i.code.value for i in report.issues if i.severity is Severity.ERROR and i.record_key is not None}


def sanitize_resources(
    resources: Sequence[StoredResource],
    config: SanitizationConfig,
    *,
    now: datetime | None = None,
) -> tuple[list[StoredResource], dict[str, int]]:
    """Sanitize without validating the output. Returns the resources (same keys) and action counts."""
    sanitizer = _Sanitizer(config, now or datetime.now(timezone.utc))
    sanitized = [StoredResource(s.key, s.file_name, sanitizer.resource(s.resource)) for s in resources]
    return sanitized, dict(sorted(sanitizer.actions.items()))


def sanitize_dataset(
    resources: Sequence[StoredResource],
    config: SanitizationConfig | None = None,
    *,
    validation_before: ValidationReport | None = None,
    now: datetime | None = None,
) -> SanitizationResult:
    """Sanitize ``resources`` and validate and re-scan the output."""
    config = config or SanitizationConfig()
    now = now or datetime.now(timezone.utc)
    sanitized, actions = sanitize_resources(resources, config, now=now)

    return SanitizationResult(
        config=config,
        resources=tuple(s.resource for s in sanitized),
        actions=actions,
        validation_before=validation_before or validate_dataset(resources, now=now),
        validation_after=validate_dataset(sanitized, now=now),
        residual=detect_phi(sanitized, now=now),
        created_at=now,
    )
