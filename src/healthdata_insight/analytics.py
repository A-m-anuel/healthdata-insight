"""Pseudonymized analytical dataset and public-health statistics.

Everything downstream of this module (public-health charts, the data
explorer, R, the analytical summary report) reads the analytical dataset,
never the uploaded resources. It is built in two layers of protection:

1. The resources are sanitized with a strict configuration
   (``ANALYTICS_CONFIG``): ids pseudonymized with a per-run random key,
   identifiers removed, addresses reduced to state/country, birth dates to
   year (ages 90+ grouped), clinical dates to month.
2. Only an allow-list of non-identifying columns is extracted from the
   sanitized resources. Free text (narrative, ``code.text``, ``valueString``,
   notes) is never extracted, so labels come from terminology codings only.

Resources with validation errors and records flagged as duplicates are
excluded, so statistics reflect valid, de-duplicated data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from healthdata_insight.config import IN_SCOPE_RESOURCE_TYPES
from healthdata_insight.fhir import parse_reference
from healthdata_insight.models import Dimension, ValidationReport
from healthdata_insight.sanitize import (
    AddressAction,
    EventDateAction,
    IdentifierAction,
    PersonDateAction,
    SanitizationConfig,
    sanitize_resources,
)
from healthdata_insight.storage import StoredResource

ANALYTICS_CONFIG = SanitizationConfig(
    identifiers=IdentifierAction.REMOVE,
    addresses=AddressAction.GENERALIZE,
    person_dates=PersonDateAction.YEAR,
    group_ages_over_89=True,
    event_dates=EventDateAction.MONTH,
    pseudonymize_ids=True,
)

AGE_GROUPS: tuple[tuple[int, int | None, str], ...] = (
    (0, 17, "0-17"), (18, 34, "18-34"), (35, 49, "35-49"), (50, 64, "50-64"), (65, 79, "65-79"), (80, None, "80+"),
)
AGE_GROUP_ORDER = [label for *_, label in AGE_GROUPS]
GENDER_ORDER = ["female", "male", "other", "unknown"]

PATIENT_COLUMNS = ["patient_key", "gender", "birth_year", "age", "age_group", "state", "country", "deceased"]
CONDITION_COLUMNS = ["condition_key", "patient_key", "condition", "code", "code_system", "clinical_status",
                     "verification_status", "onset_month", "resolved"]
OBSERVATION_COLUMNS = ["observation_key", "patient_key", "observation", "code", "code_system", "category",
                       "status", "value", "unit", "value_type", "value_label", "effective_month"]


@dataclass(frozen=True)
class AnalyticalDataset:
    patients: pd.DataFrame
    conditions: pd.DataFrame
    observations: pd.DataFrame
    excluded_invalid: int
    excluded_duplicates: int
    reference_year: int

    @property
    def record_count(self) -> int:
        return len(self.patients) + len(self.conditions) + len(self.observations)


# --------------------------------------------------------------------------- extraction helpers

def _coding(concept: Any) -> tuple[str | None, str | None, str | None]:
    """(label, code, system) of the first usable coding. Free-text ``text`` is deliberately ignored."""
    if not isinstance(concept, dict) or not isinstance(concept.get("coding"), list):
        return None, None, None
    for coding in concept["coding"]:
        if isinstance(coding, dict) and isinstance(coding.get("code"), str):
            display = coding.get("display") if isinstance(coding.get("display"), str) else None
            return display or coding["code"], coding["code"], coding.get("system")
    return None, None, None


def _patient_key(reference: Any) -> str | None:
    if not isinstance(reference, dict) or not isinstance(reference.get("reference"), str):
        return None
    parsed = parse_reference(reference["reference"])
    if parsed is None or parsed.resource_type not in (None, "Patient"):
        return None
    return parsed.resource_id


def _month(resource: dict, prefix: str) -> str | None:
    value = resource.get(f"{prefix}DateTime")
    if value is None and isinstance(resource.get(f"{prefix}Period"), dict):
        value = resource[f"{prefix}Period"].get("start")
    return value[:7] if isinstance(value, str) and len(value) >= 7 else None


def age_group(age: int | None) -> str | None:
    if age is None or age < 0:
        return None
    for low, high, label in AGE_GROUPS:
        if age >= low and (high is None or age <= high):
            return label
    return None


def _patient_row(r: dict, reference_year: int) -> dict:
    birth = r.get("birthDate")
    birth_year = int(birth[:4]) if isinstance(birth, str) and birth[:4].isdigit() else None
    death = r.get("deceasedDateTime")
    death_year = int(death[:4]) if isinstance(death, str) and death[:4].isdigit() else None
    # Age at death for deceased patients, otherwise age at the processing date.
    age = (death_year or reference_year) - birth_year if birth_year else None
    address = next((a for a in r.get("address", []) if isinstance(a, dict)), {})
    gender = r.get("gender") if r.get("gender") in GENDER_ORDER else "unknown"
    return {
        "patient_key": r.get("id"),
        "gender": gender,
        "birth_year": birth_year,
        "age": age,
        "age_group": age_group(age),
        "state": address.get("state"),
        "country": address.get("country"),
        "deceased": bool(r.get("deceasedDateTime") or r.get("deceasedBoolean")),
    }


def _condition_row(r: dict) -> dict:
    label, code, system = _coding(r.get("code"))
    clinical = _coding(r.get("clinicalStatus"))[1]
    return {
        "condition_key": r.get("id"),
        "patient_key": _patient_key(r.get("subject")),
        "condition": label or "(uncoded)",
        "code": code,
        "code_system": system,
        "clinical_status": clinical,
        "verification_status": _coding(r.get("verificationStatus"))[1],
        "onset_month": _month(r, "onset"),
        "resolved": clinical in ("inactive", "remission", "resolved") or any(k.startswith("abatement") for k in r),
    }


def _observation_row(r: dict) -> dict:
    label, code, system = _coding(r.get("code"))
    category = next((_coding(c)[1] for c in r.get("category", []) if _coding(c)[1]), None) \
        if isinstance(r.get("category"), list) else None
    value, unit, value_type, value_label = None, None, "none", None
    if isinstance(r.get("valueQuantity"), dict):
        quantity = r["valueQuantity"]
        number = quantity.get("value")
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            value, value_type = float(number), "quantity"
            unit = quantity.get("unit") or quantity.get("code")
    elif isinstance(r.get("valueInteger"), int) and not isinstance(r.get("valueInteger"), bool):
        value, value_type = float(r["valueInteger"]), "quantity"
    elif "valueCodeableConcept" in r:
        value_label, value_type = _coding(r["valueCodeableConcept"])[0], "coded"
    elif isinstance(r.get("valueBoolean"), bool):
        value_label, value_type = str(r["valueBoolean"]).lower(), "coded"
    elif "valueString" in r:
        value_type = "text (not shown)"  # free text may contain identifying details
    elif "component" in r:
        value_type = "components"
    return {
        "observation_key": r.get("id"),
        "patient_key": _patient_key(r.get("subject")),
        "observation": label or "(uncoded)",
        "code": code,
        "code_system": system,
        "category": category,
        "status": r.get("status") if isinstance(r.get("status"), str) else None,
        "value": value,
        "unit": unit,
        "value_type": value_type,
        "value_label": value_label,
        "effective_month": _month(r, "effective"),
    }


def build_analytical_dataset(
    resources: Sequence[StoredResource],
    validation: ValidationReport,
    *,
    now: datetime | None = None,
) -> AnalyticalDataset:
    now = now or datetime.now(timezone.utc)
    duplicates = {i.record_key for i in validation.issues if i.dimension is Dimension.UNIQUENESS}
    invalid = set(validation.invalid_resource_keys)
    # Only in-scope types are analyzed, so only they are copied through the sanitizer.
    usable = [s for s in resources if s.resource["resourceType"] in IN_SCOPE_RESOURCE_TYPES
              and s.key not in invalid and s.key not in duplicates]
    sanitized, _ = sanitize_resources(usable, ANALYTICS_CONFIG, now=now)

    rows: dict[str, list[dict]] = {"Patient": [], "Condition": [], "Observation": []}
    for stored in sanitized:
        r = stored.resource
        if r["resourceType"] == "Patient":
            rows["Patient"].append(_patient_row(r, now.year))
        elif r["resourceType"] == "Condition":
            rows["Condition"].append(_condition_row(r))
        elif r["resourceType"] == "Observation":
            rows["Observation"].append(_observation_row(r))

    return AnalyticalDataset(
        patients=pd.DataFrame(rows["Patient"], columns=PATIENT_COLUMNS).astype({"birth_year": "Int64", "age": "Int64"}),
        conditions=pd.DataFrame(rows["Condition"], columns=CONDITION_COLUMNS),
        observations=pd.DataFrame(rows["Observation"], columns=OBSERVATION_COLUMNS).astype({"value": "float64"}),
        excluded_invalid=sum(1 for s in resources if s.key in invalid),
        excluded_duplicates=sum(1 for s in resources if s.key in duplicates and s.key not in invalid),
        reference_year=now.year,
    )


# --------------------------------------------------------------------------- filtering

@dataclass(frozen=True)
class CohortFilter:
    genders: tuple[str, ...] = ()
    age_groups: tuple[str, ...] = ()
    states: tuple[str, ...] = ()

    def apply(self, dataset: AnalyticalDataset) -> AnalyticalDataset:
        patients = dataset.patients
        if self.genders:
            patients = patients[patients["gender"].isin(self.genders)]
        if self.age_groups:
            patients = patients[patients["age_group"].isin(self.age_groups)]
        if self.states:
            patients = patients[patients["state"].isin(self.states)]
        if len(patients) == len(dataset.patients):
            return dataset
        keys = set(patients["patient_key"])
        return AnalyticalDataset(
            patients=patients,
            conditions=dataset.conditions[dataset.conditions["patient_key"].isin(keys)],
            observations=dataset.observations[dataset.observations["patient_key"].isin(keys)],
            excluded_invalid=dataset.excluded_invalid,
            excluded_duplicates=dataset.excluded_duplicates,
            reference_year=dataset.reference_year,
        )


# --------------------------------------------------------------------------- statistics

def gender_distribution(dataset: AnalyticalDataset) -> pd.DataFrame:
    counts = dataset.patients["gender"].value_counts()
    rows = [{"gender": g, "patients": int(counts.get(g, 0))} for g in GENDER_ORDER if counts.get(g, 0)]
    return _with_share(pd.DataFrame(rows, columns=["gender", "patients"]), "patients")


def age_distribution(dataset: AnalyticalDataset) -> pd.DataFrame:
    """Patients per age group and gender (long format, age groups in order)."""
    patients = dataset.patients.dropna(subset=["age_group"])
    grouped = patients.groupby(["age_group", "gender"]).size().reset_index(name="patients")
    grouped["age_group"] = pd.Categorical(grouped["age_group"], AGE_GROUP_ORDER, ordered=True)
    return grouped.sort_values(["age_group", "gender"], ignore_index=True)


def geography(dataset: AnalyticalDataset) -> pd.DataFrame:
    counts = dataset.patients["state"].fillna("(not recorded)").value_counts()
    return _with_share(counts.rename_axis("state").reset_index(name="patients"), "patients")


def condition_frequencies(dataset: AnalyticalDataset) -> pd.DataFrame:
    """Per condition: records, distinct patients, prevalence among patients, and active share."""
    columns = ["condition", "code", "records", "patients", "prevalence_pct", "active"]
    conditions = dataset.conditions
    if conditions.empty:
        return pd.DataFrame(columns=columns)
    total_patients = max(len(dataset.patients), 1)
    grouped = conditions.groupby(["condition", "code"], dropna=False).agg(
        records=("condition_key", "size"),
        patients=("patient_key", "nunique"),
        active=("resolved", lambda s: int((~s.astype(bool)).sum())),
    ).reset_index()
    grouped["prevalence_pct"] = 100 * grouped["patients"] / total_patients
    return grouped.sort_values(["patients", "records"], ascending=False, ignore_index=True)[columns]


def observation_statistics(dataset: AnalyticalDataset) -> pd.DataFrame:
    """Descriptive statistics per numeric observation type (one row per code and unit)."""
    columns = ["observation", "code", "unit", "n", "patients", "mean", "sd", "min", "p25", "median", "p75", "max"]
    numeric = dataset.observations.dropna(subset=["value"])
    if numeric.empty:
        return pd.DataFrame(columns=columns)
    grouped = numeric.groupby(["observation", "code", "unit"], dropna=False)
    stats = grouped["value"].describe().rename(columns={"count": "n", "std": "sd", "25%": "p25", "50%": "median",
                                                        "75%": "p75"})
    stats["patients"] = grouped["patient_key"].nunique()
    stats = stats.reset_index().astype({"n": "int64"})
    return stats.sort_values("n", ascending=False, ignore_index=True)[columns]


def temporal_trend(months: pd.Series, *, max_monthly_points: int = 36) -> pd.DataFrame:
    """Counts per period from ``YYYY-MM`` strings; monthly if the span is short enough, else yearly."""
    months = months.dropna()
    if months.empty:
        return pd.DataFrame(columns=["period", "count", "granularity"])
    periods = pd.PeriodIndex(months, freq="M")
    span = (periods.max() - periods.min()).n + 1
    if span > max_monthly_points:
        periods, granularity = periods.asfreq("Y"), "year"
    else:
        granularity = "month"
    counts = pd.Series(1, index=periods).groupby(level=0).sum()
    full = pd.period_range(counts.index.min(), counts.index.max(), freq=counts.index.freq)
    counts = counts.reindex(full, fill_value=0)
    return pd.DataFrame({
        "period": counts.index.to_timestamp(),
        "count": counts.to_numpy(),
        "granularity": granularity,
    })


def _with_share(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    total = frame[column].sum()
    frame["share_pct"] = 100 * frame[column] / total if total else 0.0
    return frame
