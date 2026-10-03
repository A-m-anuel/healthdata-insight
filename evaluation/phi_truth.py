"""Ground truth for the PHI-detection experiment.

Labels come from a written reference policy (below), applied by this module without calling the
detector. The policy is modelled on the identifier categories in the HIPAA Safe Harbor
de-identification method (names; geographic units smaller than a state; date elements other than year,
and ages over 89; telephone numbers and e-mail addresses; record, account, licence, and other
identifying numbers; photographs). It is a labelling yardstick for this experiment only; it is not a
compliance determination.

Unit of evaluation: a *location* - one element instance in one resource, written with the same path
convention the detector uses (e.g. ``Patient.address[0].postalCode``).

Two kinds of locations are labelled:

* Locations that exist in the Synthea data (Patient fields, narratives, reference display text).
  Free text is labelled PHI when it contains an identifying value of that file's patient.
* Planted locations, added under the control of a seeded RNG, to measure what the detector does with
  identifying text outside the fields it targets, and with decoys that look similar but aren't PHI.
"""

from __future__ import annotations

import copy
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from healthdata_insight.fhir import age_reference_year
from healthdata_insight.storage import StoredResource

BIRTH_PLACE = "http://hl7.org/fhir/StructureDefinition/patient-birthPlace"
MAIDEN_NAME = "http://hl7.org/fhir/StructureDefinition/patient-mothersMaidenName"
GEOLOCATION = "http://hl7.org/fhir/StructureDefinition/geolocation"

STRUCTURED, REFERENCE_DISPLAY, NARRATIVE, FREE_TEXT = "structured", "reference display", "narrative", "free text"


@dataclass(frozen=True)
class Candidate:
    key: int
    path: str
    is_phi: bool
    scope: str
    reason: str
    planted: bool = False


def _filled(value) -> bool:
    return value not in (None, "", [], {})


def identity_values(patient: dict) -> set[str]:
    """Identifying strings of one patient, used to label free text (narratives, display text).

    Names are matched as *full* names: Synthea clinicians often share a surname with their patients, so a
    surname alone does not show that a text is about the patient. City names are left out for the same
    reason (they occur in hospital names and addresses).
    """
    values: set[str] = set()
    for name in patient.get("name", []):
        given, family = name.get("given", []), name.get("family")
        if given and family:
            values.add(f"{' '.join(given)} {family}")
            values.add(f"{given[0]} {family}")
    values.update(t["value"] for t in patient.get("telecom", []) if t.get("value"))
    values.update(i["value"] for i in patient.get("identifier", []) if i.get("value"))
    for address in patient.get("address", []):
        values.update(address.get("line", []))
        if address.get("postalCode"):
            values.add(address["postalCode"])
    return {v for v in values if isinstance(v, str) and len(v) >= 4}


def mentions(text: str, values: set[str]) -> bool:
    """Whole-token match, so a postal code inside a longer number or id doesn't count."""
    return any(re.search(rf"(?<![0-9A-Za-z]){re.escape(v)}(?![0-9A-Za-z])", text) for v in values)


def _date_is_phi(value: str, age_year: int, *, birth: bool) -> bool:
    """Day or month precision is identifying; a bare birth year only for ages 90+ (at death, if deceased)."""
    if len(value) > 4:
        return True
    return birth and value.isdigit() and age_year - int(value) >= 90


def _patient_candidates(stored: StoredResource, now: datetime) -> list[Candidate]:
    r, key = stored.resource, stored.key
    out: list[Candidate] = []

    def add(path: str, is_phi: bool, reason: str) -> None:
        out.append(Candidate(key, f"Patient.{path}", is_phi, STRUCTURED, reason))

    for i, name in enumerate(r.get("name", [])):
        if any(_filled(name.get(k)) for k in ("family", "given", "text")):
            add(f"name[{i}]", True, "name")
    for i, telecom in enumerate(r.get("telecom", [])):
        if _filled(telecom.get("value")):
            add(f"telecom[{i}]", True, "telephone / e-mail")
    for i, identifier in enumerate(r.get("identifier", [])):
        if _filled(identifier.get("value")):
            add(f"identifier[{i}]", True, "record or identifying number")
    for i, address in enumerate(r.get("address", [])):
        if _filled(address.get("line")) or _filled(address.get("text")):
            add(f"address[{i}].line", True, "street address")
        if _filled(address.get("city")) or _filled(address.get("district")):
            add(f"address[{i}].city", True, "city (smaller than a state)")
        if _filled(address.get("postalCode")):
            add(f"address[{i}].postalCode", True, "postal code")
        if _filled(address.get("state")) or _filled(address.get("country")):
            add(f"address[{i}].state", False, "state / country: not smaller than a state")
        if any(e.get("url") == GEOLOCATION for e in address.get("extension", []) if isinstance(e, dict)):
            add(f"address[{i}].extension", True, "latitude / longitude")
    if isinstance(r.get("birthDate"), str):
        add("birthDate", _date_is_phi(r["birthDate"], age_reference_year(r, now), birth=True),
            "birth date (day/month, or age 90+)")
    if isinstance(r.get("deceasedDateTime"), str):
        add("deceasedDateTime", _date_is_phi(r["deceasedDateTime"], now.year, birth=False), "date of death")
    for i, extension in enumerate(r.get("extension", [])):
        url = extension.get("url") if isinstance(extension, dict) else None
        if url == BIRTH_PLACE:
            add(f"extension[{i}]", _filled(extension.get("valueAddress", {}).get("city")), "birth place (city)")
        elif url == MAIDEN_NAME:
            add(f"extension[{i}]", True, "name of a relative")
        else:
            add(f"extension[{i}]", False, f"non-identifying extension ({str(url).rsplit('/', 1)[-1]})")
    for i, _ in enumerate(r.get("photo", [])):
        add(f"photo[{i}]", True, "photograph")
    for i, contact in enumerate(r.get("contact", [])):
        for part in ("name", "telecom", "address"):
            if _filled(contact.get(part)):
                add(f"contact[{i}].{part}", True, f"relative's {part}")
    for field in ("gender", "maritalStatus", "communication", "multipleBirthBoolean", "multipleBirthInteger"):
        if field in r:
            add(field, False, "not an identifier category")
    return out


def label_dataset(resources: Sequence[StoredResource], now: datetime) -> list[Candidate]:
    """Label every candidate location in the (possibly planted) dataset, except planted ones."""
    identities = {s.file_name: identity_values(s.resource) for s in resources if s.resource["resourceType"] == "Patient"}
    candidates: list[Candidate] = []
    for stored in resources:
        r, key, rtype = stored.resource, stored.key, stored.resource["resourceType"]
        own = identities.get(stored.file_name, set())
        if rtype == "Patient":
            candidates.extend(_patient_candidates(stored, now))
        text = r.get("text")
        if isinstance(text, dict) and _filled(text.get("div")):
            phi = mentions(text["div"], own)
            candidates.append(Candidate(key, f"{rtype}.text.div", phi, NARRATIVE,
                                        "narrative mentioning the patient" if phi
                                        else "narrative without identifying content"))
        for field, value in r.items():
            if isinstance(value, dict) and isinstance(value.get("display"), str) and value["display"]:
                phi = mentions(value["display"], own)
                candidates.append(Candidate(key, f"{rtype}.{field}.display", phi, REFERENCE_DISPLAY,
                                            "display text naming the patient" if phi
                                            else "display text of another party"))
    return candidates


# --------------------------------------------------------------------------- planting

PLANTS = (
    ("observation_narrative_with_name", "Observation", NARRATIVE, True),
    ("observation_narrative_decoy", "Observation", NARRATIVE, False),
    ("observation_free_text_value", "Observation", FREE_TEXT, True),
    ("condition_note_with_address", "Condition", FREE_TEXT, True),
    ("condition_subject_display_name", "Condition", REFERENCE_DISPLAY, True),
    ("condition_asserter_display_decoy", "Condition", REFERENCE_DISPLAY, False),
)


def plant(
    resources: Sequence[StoredResource],
    rng: random.Random,
) -> tuple[list[StoredResource], list[Candidate]]:
    """For each patient, plant one instance of each PLANTS item on distinct resources of that patient's file.

    Returns the modified dataset and the planted candidates (with their labels). Planted resources are
    excluded from ``label_dataset``'s output by the caller (planted labels take precedence).
    """
    patients = {s.file_name: s.resource for s in resources if s.resource["resourceType"] == "Patient"}
    by_file: dict[tuple[str, str], list[int]] = {}
    for index, stored in enumerate(resources):
        by_file.setdefault((stored.file_name, stored.resource["resourceType"]), []).append(index)

    out = list(resources)
    planted: list[Candidate] = []
    for file_name, patient in sorted(patients.items()):
        name = patient.get("name", [{}])[0]
        full_name = " ".join([*name.get("given", []), name.get("family", "")]).strip()
        phone = next((t["value"] for t in patient.get("telecom", []) if t.get("value")), None)
        street = next((a["line"][0] for a in patient.get("address", []) if a.get("line")), None)
        if not full_name or not phone or not street:
            continue
        pools = {t: list(by_file.get((file_name, t), [])) for t in ("Observation", "Condition")}
        for pool in pools.values():
            rng.shuffle(pool)
        for item, rtype, scope, is_phi in PLANTS:
            if not pools[rtype]:
                continue
            index = pools[rtype].pop()
            r = copy.deepcopy(out[index].resource)
            if item == "observation_narrative_with_name":
                r["text"] = {"status": "generated", "div": f"<div>Measurement for {full_name}</div>"}
                path = "Observation.text.div"
            elif item == "observation_narrative_decoy":
                r["text"] = {"status": "generated", "div": "<div>Routine vital sign measurement</div>"}
                path = "Observation.text.div"
            elif item == "observation_free_text_value":
                for value_key in [k for k in r if k.startswith("value")]:
                    del r[value_key]
                r["valueString"] = f"{full_name} reported symptoms by phone ({phone})"
                path = "Observation.valueString"
            elif item == "condition_note_with_address":
                r["note"] = [{"text": f"Patient lives at {street}; follow up at home."}]
                path = "Condition.note"
            elif item == "condition_subject_display_name":
                r.setdefault("subject", {})["display"] = full_name
                path = "Condition.subject.display"
            else:
                r["asserter"] = {"reference": "Practitioner/eval-practitioner", "display": "Dr. Eval Example"}
                path = "Condition.asserter.display"
            out[index] = StoredResource(out[index].key, out[index].file_name, r)
            planted.append(Candidate(out[index].key, path, is_phi, scope, item, planted=True))
    return out, planted
