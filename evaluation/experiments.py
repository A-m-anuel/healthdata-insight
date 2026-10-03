"""Batch-level experiments. Each function evaluates one batch and returns plain, JSON-serializable counts.

The orchestration (``run_evaluation.py``) aggregates the per-batch counts; nothing here prints or
writes files.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone

from healthdata_insight.analytics import AGE_GROUPS, build_analytical_dataset
from healthdata_insight.config import IN_SCOPE_RESOURCE_TYPES
from healthdata_insight.fhir import parse_reference
from healthdata_insight.models import Dimension, IssueCode, Severity, ValidationReport
from healthdata_insight.parser import parse_fhir_file
from healthdata_insight.phi import detect_phi
from healthdata_insight.quality import assess_quality
from healthdata_insight.sanitize import (
    AddressAction,
    EventDateAction,
    IdentifierAction,
    PersonDateAction,
    SanitizationConfig,
    sanitize_dataset,
)
from healthdata_insight.storage import StoredResource
from healthdata_insight.validation import validate_dataset

from evaluation import defects, phi_truth

EVAL_NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)  # fixed reference date: ages and "future" checks
SEED = 20261003
PER_DEFECT = 25  # injected instances per defect type per batch
PER_DUPLICATE_CASE = 10

SANITIZATION_CONFIGS = {
    "default": SanitizationConfig(secret="evaluation-fixed-secret"),
    "strict": SanitizationConfig(identifiers=IdentifierAction.REMOVE, addresses=AddressAction.REMOVE,
                                 person_dates=PersonDateAction.REMOVE, event_dates=EventDateAction.YEAR,
                                 secret="evaluation-fixed-secret"),
}


def _codes_by_key(report: ValidationReport) -> dict[int, set[IssueCode]]:
    out: dict[int, set[IssueCode]] = {}
    for issue in report.issues:
        if issue.record_key is not None:
            out.setdefault(issue.record_key, set()).add(issue.code)
    return out


# --------------------------------------------------------------------------- baseline

def baseline(resources: Sequence[StoredResource], parse_issues, report: ValidationReport) -> dict:
    quality = assess_quality(report)
    by_code = Counter((i.severity.value, i.code.value, i.resource_type or "-") for i in report.issues
                      if i.code is not IssueCode.UNSUPPORTED_RESOURCE_TYPE)
    errors = [i for i in report.issues if i.severity is Severity.ERROR]
    return {
        "resources": len(resources),
        "in_scope": report.in_scope_count,
        "by_type": dict(Counter(s.resource["resourceType"] for s in resources)),
        "errors": len(errors),
        "warnings": report.warning_count,
        "invalid_records": report.invalid_record_count,
        "error_examples": [f"{i.code.value}: {i.message}" for i in errors[:10]],
        "issues_by_code": {"|".join(k): v for k, v in by_code.items()},
        "unsupported_type_issues": sum(i.code is IssueCode.UNSUPPORTED_RESOURCE_TYPE for i in report.issues),
        "expected_fields": sum(f.expected for f in report.field_completeness),
        "present_fields": sum(f.present for f in report.field_completeness),
        "duplicates": quality.duplicate_count,
        "dimension_passed_total": {d.dimension.value: [d.passed, d.total] for d in quality.dimensions},
        "parse_issues": len(parse_issues),
    }


# --------------------------------------------------------------------------- E1 / E3: injected defects

def injected_defects(
    resources: Sequence[StoredResource],
    parse_issues,
    base: ValidationReport,
    catalog: Sequence[defects.Defect],
    rng: random.Random,
) -> dict:
    """Detection per defect type, each injected and validated in its own pass.

    Isolation matters: in a combined pass one defect can mask another (e.g. removing a Patient's
    birthDate makes "observation before birth" undetectable for that patient's observations).
    A final combined pass measures how the dataset-level quality scores respond.
    """
    quality_before = assess_quality(base)
    per_defect: dict[str, Counter] = {}
    clean_records = clean_flagged = 0
    completeness_passes = completeness_exact = 0
    for defect in catalog:
        mutated, injections = defects.inject(resources, [defect], PER_DEFECT, rng)
        report = validate_dataset(mutated, parse_issues, now=EVAL_NOW)
        codes = _codes_by_key(report)
        injected_keys = {i.key for i in injections}
        stats = per_defect.setdefault(defect.name, Counter())
        for injection in injections:
            found = codes.get(injection.key, set())
            stats["injected"] += 1
            stats["detected"] += defect.expected in found
            stats["flagged_invalid"] += injection.key in report.invalid_resource_keys
            stats["other_codes"] += len(found - {defect.expected})
        clean = [s.key for s in resources if s.key not in injected_keys]
        clean_records += len(clean)
        clean_flagged += sum(k in report.invalid_resource_keys for k in clean)
        if defect.name in defects.COMPLETENESS_DEFECTS:
            completeness_passes += 1
            increase = assess_quality(report).missing_field_count - quality_before.missing_field_count
            completeness_exact += increase == len(injections)
            stats["missing_field_increase"] += increase

    mutated, _ = defects.inject(resources, catalog, PER_DEFECT, rng)
    quality_after = assess_quality(validate_dataset(mutated, parse_issues, now=EVAL_NOW))
    return {
        "per_defect": {name: dict(c) for name, c in per_defect.items()},
        "clean_records": clean_records,
        "clean_flagged_invalid": clean_flagged,
        "completeness_passes": completeness_passes,
        "completeness_passes_exact": completeness_exact,
        "scores_before": {d.dimension.value: d.percent for d in quality_before.dimensions},
        "scores_after": {d.dimension.value: d.percent for d in quality_after.dimensions},
    }


def malformed_lines(resources: Sequence[StoredResource], rng: random.Random, n_each: int = 50) -> dict:
    """NDJSON with known numbers of truncated, untyped, and non-object lines."""
    observations = [s.resource for s in resources if s.resource["resourceType"] == "Observation"][:1000]
    lines = [json.dumps(r, separators=(",", ":")) for r in observations]
    picks = rng.sample(range(len(lines)), 3 * n_each)
    for index in picks[:n_each]:
        lines[index] = lines[index][: len(lines[index]) // 2]  # truncated JSON
    for index in picks[n_each:2 * n_each]:
        lines[index] = lines[index].replace('"resourceType":"Observation",', "", 1)
    for index in picks[2 * n_each:]:
        lines[index] = json.dumps("a JSON string, not a resource")
    parsed = parse_fhir_file("malformed.ndjson", "\n".join(lines).encode("utf-8"))
    found = Counter(i.code.value for i in parsed.issues)
    return {
        "lines": len(lines),
        "injected": {"malformed-json": n_each, "missing-resource-type": n_each, "invalid-structure": n_each},
        "detected": {code: found.get(code, 0) for code in ("malformed-json", "missing-resource-type",
                                                           "invalid-structure")},
        "resources_read": len(parsed.resources),
        "expected_resources_read": len(lines) - 3 * n_each,
    }


# --------------------------------------------------------------------------- E4: duplicates

def duplicates(resources: Sequence[StoredResource], parse_issues, base: ValidationReport,
               rng: random.Random) -> dict:
    augmented, injections = defects.inject_duplicates(resources, PER_DUPLICATE_CASE, rng)
    report = validate_dataset(augmented, parse_issues, now=EVAL_NOW)
    unique_issues: dict[int, set[IssueCode]] = {}
    for issue in report.issues:
        if issue.dimension is Dimension.UNIQUENESS and issue.record_key is not None:
            unique_issues.setdefault(issue.record_key, set()).add(issue.code)
    per_case: dict[str, Counter] = {}
    for injection in injections:
        stats = per_case.setdefault(injection.case.name, Counter())
        found = unique_issues.get(injection.key, set())
        stats["injected"] += 1
        stats["flagged"] += bool(found)
        stats["expected_code"] += injection.case.expected in found if injection.case.expected else 0
    baseline_dups = Counter((i.resource_type, i.code.value) for i in base.issues
                            if i.dimension is Dimension.UNIQUENESS)
    return {
        "per_case": {name: dict(c) for name, c in per_case.items()},
        "truth": {c.name: c.is_duplicate for c in defects.DUPLICATE_CASES},
        "baseline_flagged": {f"{t}|{c}": n for (t, c), n in baseline_dups.items()},
    }


# --------------------------------------------------------------------------- E2: PHI

def phi(resources: Sequence[StoredResource], rng: random.Random) -> dict:
    planted, planted_candidates = phi_truth.plant(resources, rng)
    labels = {(c.key, c.path): c for c in phi_truth.label_dataset(planted, EVAL_NOW)}
    labels.update({(c.key, c.path): c for c in planted_candidates})  # planted labels take precedence

    started = time.perf_counter()
    report = detect_phi(planted, now=EVAL_NOW)
    seconds = time.perf_counter() - started
    findings = {}
    for f in report.findings:
        findings.setdefault((f.record_key, f.path), f)

    counts: Counter = Counter()
    unexpected: Counter = Counter()
    for location, candidate in labels.items():
        flagged = location in findings
        group = (candidate.scope, candidate.reason, candidate.is_phi)
        counts[(*group, flagged)] += 1
    for location, finding in findings.items():
        if location not in labels:
            unexpected[(finding.rule, finding.path.split("[")[0])] += 1
    by_severity: Counter = Counter()
    for location, finding in findings.items():
        candidate = labels.get(location)
        by_severity[(finding.severity.value, bool(candidate and candidate.is_phi))] += 1
    return {
        "counts": [[scope, reason, is_phi, flagged, n] for (scope, reason, is_phi, flagged), n in counts.items()],
        "unexpected_findings": {"|".join(k): v for k, v in unexpected.items()},
        "findings_by_severity_and_truth": [[sev, truth, n] for (sev, truth), n in by_severity.items()],
        "detector_seconds": seconds,
        "resources_scanned": len(planted),
    }


# --------------------------------------------------------------------------- E7: sanitization

def _identity_strings(patient: dict) -> dict[str, set[str]]:
    kinds: dict[str, set[str]] = {"name": set(), "telecom": set(), "identifier": set(), "street": set(),
                                  "postal code": set(), "full birth date": set()}
    for name in patient.get("name", []):
        kinds["name"].update(v for v in [*name.get("given", []), name.get("family")] if v)
    kinds["telecom"].update(t["value"] for t in patient.get("telecom", []) if t.get("value"))
    kinds["identifier"].update(i["value"] for i in patient.get("identifier", []) if i.get("value"))
    for address in patient.get("address", []):
        kinds["street"].update(address.get("line", []))
        if address.get("postalCode"):
            kinds["postal code"].add(address["postalCode"])
    if isinstance(patient.get("birthDate"), str) and len(patient["birthDate"]) == 10:
        kinds["full birth date"].add(patient["birthDate"])
    return kinds


def sanitization(resources: Sequence[StoredResource], base: ValidationReport) -> dict:
    out = {}
    patients = {s.file_name: s.resource for s in resources if s.resource["resourceType"] == "Patient"}
    for name, config in SANITIZATION_CONFIGS.items():
        started = time.perf_counter()
        result = sanitize_dataset(resources, config, validation_before=base, now=EVAL_NOW)
        seconds = time.perf_counter() - started

        # Search each patient's own sanitized resources for that patient's original values, and record
        # which resource types still contain them.
        texts: dict[str, list[tuple[str, str]]] = {}
        for stored, sanitized in zip(resources, result.resources):
            texts.setdefault(stored.file_name, []).append(
                (sanitized["resourceType"], json.dumps(sanitized, separators=(",", ":"))))
        leaks: Counter = Counter()
        checked: Counter = Counter()
        leak_locations: Counter = Counter()
        for file_name, patient in patients.items():
            own = texts.get(file_name, [])
            for kind, values in _identity_strings(patient).items():
                for value in values:
                    checked[kind] += 1
                    # Whole-token match: a 5-digit postal code can occur by chance inside pseudonym hex.
                    pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z])")
                    holders = {rtype for rtype, text in own if pattern.search(text)}
                    leaks[kind] += bool(holders)
                    leak_locations.update(f"{kind}|{rtype}" for rtype in holders)
        output_bytes = sum(len(text) + 1 for chunk in texts.values() for _, text in chunk)
        round_trip = None
        if name == "default":
            parsed = parse_fhir_file("sanitized.ndjson", result.to_ndjson())
            round_trip = {"resources_read": len(parsed.resources), "parse_issues": len(parsed.issues)}
        out[name] = {
            "seconds": seconds,
            "resources_in": len(resources),
            "resources_out": len(result.resources),
            "output_bytes": output_bytes,
            "actions": result.actions,
            "residual_by_rule_severity": dict(Counter(f"{f.rule}|{f.severity.value}"
                                                      for f in result.residual.findings)),
            "residual_high": result.residual_high_risk,
            "errors_before": result.errors_before,
            "errors_after": result.errors_after,
            "new_error_codes": sorted(result.new_error_codes),
            "reference_issues_before": result.reference_issues_before,
            "reference_issues_after": result.reference_issues_after,
            "leak_checks": dict(checked),
            "leaks": dict(leaks),
            "leak_locations": dict(leak_locations),
            "round_trip": round_trip,
        }
        del result, texts
    return out


# --------------------------------------------------------------------------- E8: information preserved

def _exact_age(patient: dict, now: datetime) -> int:
    """Exact age in completed years: at death for deceased patients, otherwise at ``now``."""
    birth = patient["birthDate"]
    death = patient.get("deceasedDateTime")
    at = datetime.fromisoformat(death[:10]) if isinstance(death, str) and len(death) >= 10 else now
    year, month, day = int(birth[:4]), int(birth[5:7]), int(birth[8:10])
    return at.year - year - ((at.month, at.day) < (month, day))


def _age_group(age: int) -> str:
    for low, high, label in AGE_GROUPS:
        if age >= low and (high is None or age <= high):
            return label
    raise ValueError(age)


def _subject_id(r: dict) -> str | None:
    ref = r.get("subject", {}).get("reference") if isinstance(r.get("subject"), dict) else None
    parsed = parse_reference(ref) if isinstance(ref, str) else None
    return parsed.resource_id if parsed else None


def _code(concept) -> str | None:
    for coding in (concept or {}).get("coding", []) if isinstance(concept, dict) else []:
        if coding.get("code"):
            return coding["code"]
    return None


def information(resources: Sequence[StoredResource], base: ValidationReport) -> dict:
    """Raw (exact) values vs the de-identified analytical dataset, for the same valid, unique records."""
    analytics = build_analytical_dataset(resources, base, now=EVAL_NOW)
    excluded = set(base.invalid_resource_keys) | {i.record_key for i in base.issues
                                                  if i.dimension is Dimension.UNIQUENESS}
    usable = [s.resource for s in resources if s.key not in excluded
              and s.resource["resourceType"] in IN_SCOPE_RESOURCE_TYPES]
    raw_patients = [r for r in usable if r["resourceType"] == "Patient"]
    raw_conditions = [r for r in usable if r["resourceType"] == "Condition"]
    raw_observations = [r for r in usable if r["resourceType"] == "Observation"]

    # Rows of the analytical dataset are in resource order, so they align with the raw lists.
    exact_groups = [_age_group(_exact_age(p, EVAL_NOW)) for p in raw_patients]
    analytic_groups = list(analytics.patients["age_group"])
    raw_quantities = [(_code(o.get("code")), o["valueQuantity"].get("unit"), float(o["valueQuantity"]["value"]))
                      for o in raw_observations
                      if isinstance(o.get("valueQuantity"), dict)
                      and isinstance(o["valueQuantity"].get("value"), (int, float))]
    analytic_quantities = [(c, u, float(v)) for c, u, v in
                           analytics.observations[["code", "unit", "value"]].dropna(subset=["value"])
                           .itertuples(index=False)]
    raw_obs_months = Counter(o.get("effectiveDateTime", "")[:7] for o in raw_observations if o.get("effectiveDateTime"))
    analytic_obs_months = Counter(analytics.observations["effective_month"].dropna())
    raw_cond_months = Counter(c.get("onsetDateTime", "")[:7] for c in raw_conditions if c.get("onsetDateTime"))
    analytic_cond_months = Counter(analytics.conditions["onset_month"].dropna())
    raw_patient_ids = {p["id"] for p in raw_patients}
    return {
        "patients": [len(raw_patients), len(analytics.patients)],
        "conditions": [len(raw_conditions), len(analytics.conditions)],
        "observations": [len(raw_observations), len(analytics.observations)],
        "gender_raw": dict(Counter(p.get("gender") for p in raw_patients)),
        "gender_analytic": dict(Counter(analytics.patients["gender"])),
        "age_group_raw": dict(Counter(exact_groups)),
        "age_group_analytic": dict(Counter(analytic_groups)),
        "age_group_agreement": sum(a == b for a, b in zip(exact_groups, analytic_groups)),
        "state_raw": dict(Counter(a.get("state") for p in raw_patients for a in p.get("address", [])[:1])),
        "state_analytic": dict(Counter(analytics.patients["state"].dropna())),
        "condition_patients_raw": {code: len(ids) for code, ids in _group_ids(
            (_code(c.get("code")), _subject_id(c)) for c in raw_conditions).items()},
        "condition_patients_analytic": {code: len(ids) for code, ids in _group_ids(
            analytics.conditions[["code", "patient_key"]].itertuples(index=False)).items()},
        "quantity_sum_raw": _sum_by_code(raw_quantities),
        "quantity_sum_analytic": _sum_by_code(analytic_quantities),
        "observation_months_raw": dict(raw_obs_months),
        "observation_months_analytic": dict(analytic_obs_months),
        "condition_months_raw": dict(raw_cond_months),
        "condition_months_analytic": dict(analytic_cond_months),
        # Raw value sets for exact pooling across batches; stripped before results are written.
        "_sets": {
            "cities": {a.get("city") for p in raw_patients for a in p.get("address", [])[:1] if a.get("city")},
            "postal_codes": {a.get("postalCode") for p in raw_patients for a in p.get("address", [])[:1]
                             if a.get("postalCode")},
            "observation_days": {o["effectiveDateTime"][:10] for o in raw_observations
                                 if isinstance(o.get("effectiveDateTime"), str)},
            "analytic_observation_periods": set(analytics.observations["effective_month"].dropna()),
        },
        "linked_conditions_raw": sum(_subject_id(c) in raw_patient_ids for c in raw_conditions),
        "linked_conditions_analytic": int(analytics.conditions["patient_key"]
                                          .isin(set(analytics.patients["patient_key"])).sum()),
        "linked_observations_raw": sum(_subject_id(o) in raw_patient_ids for o in raw_observations),
        "linked_observations_analytic": int(analytics.observations["patient_key"]
                                            .isin(set(analytics.patients["patient_key"])).sum()),
    }


def _group_ids(pairs) -> dict[str, set]:
    out: dict[str, set] = {}
    for code, key in pairs:
        if code is not None and key is not None:
            out.setdefault(code, set()).add(key)
    return out


def _sum_by_code(rows) -> dict[str, list[float]]:
    """code|unit -> [n, sum, sum of squares], so means and SDs can be pooled across batches."""
    out: dict[str, list[float]] = {}
    for code, unit, value in rows:
        stats = out.setdefault(f"{code}|{unit}", [0, 0.0, 0.0])
        stats[0] += 1
        stats[1] += value
        stats[2] += value * value
    return out
