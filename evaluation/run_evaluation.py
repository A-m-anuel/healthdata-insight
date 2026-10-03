"""Run the full evaluation and write ``evaluation/results/results.json`` and ``results.md``.

Usage:
    python -m evaluation.run_evaluation                 # all 1,180 patients, batches of 150
    python -m evaluation.run_evaluation --patients 40 --batch-size 20 --perf-sizes 10 --perf-repeats 1

Every number in EVALUATION.md comes from these output files.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import random
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from healthdata_insight.persistence import RunRepository
from healthdata_insight.pipeline import process_dataset
from healthdata_insight.reports import to_json

from evaluation import defects, experiments
from evaluation.data import DATA_SHA256, DATA_URL, batches, ensure_dataset, load_batch, patient_files
from evaluation.report import render_markdown
from evaluation.sysinfo import environment, free_commit_gb, peak_memory_mb

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _seeded(batch: int, experiment: str) -> random.Random:
    return random.Random(f"{experiments.SEED}-{batch}-{experiment}")


def _merge_counters(target: dict, source: dict) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _prf(tp: int, fp: int, fn: int, tn: int | None = None) -> dict:
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    out = {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}
    if tn is not None:
        out |= {"tn": tn, "accuracy": _ratio(tp + tn, tp + fp + fn + tn), "specificity": _ratio(tn, tn + fp)}
    return out


# --------------------------------------------------------------------------- batch loop

def run_batches(archive: zipfile.ZipFile, names: list[str], batch_size: int) -> dict:
    per_batch: list[dict] = []
    sets: dict[str, set] = {}
    for number, batch_names in enumerate(batches(names, batch_size)):
        started = time.perf_counter()
        loaded = load_batch(archive, batch_names)
        store = loaded.store
        with tempfile.TemporaryDirectory() as tmp:
            repo = RunRepository(Path(tmp) / "history.db")
            pipeline_started = time.perf_counter()
            result = process_dataset(store, repo, now=experiments.EVAL_NOW)
            pipeline_seconds = time.perf_counter() - pipeline_started
        base = result.analysis.validation
        r_status = result.r_output.status.value
        del result
        gc.collect()

        resources, parse_issues = store.resources(), store.parse_issues()
        entry = {
            "batch": number,
            "patients": len(batch_names),
            "bytes": loaded.bytes,
            "ingest_seconds": loaded.ingest_seconds,
            "pipeline_seconds": pipeline_seconds,
            "r_status": r_status,
            "baseline": experiments.baseline(resources, parse_issues, base),
            "validation_defects": experiments.injected_defects(
                resources, parse_issues, base, defects.VALIDATION_DEFECTS, _seeded(number, "validation")),
            "quality_defects": experiments.injected_defects(
                resources, parse_issues, base, defects.QUALITY_DEFECTS, _seeded(number, "quality")),
            "duplicates": experiments.duplicates(resources, parse_issues, base, _seeded(number, "duplicates")),
            "phi": experiments.phi(resources, _seeded(number, "phi")),
            "sanitization": experiments.sanitization(resources, base),
            "information": experiments.information(resources, base),
        }
        if number == 0:
            entry["malformed_lines"] = experiments.malformed_lines(resources, _seeded(number, "malformed"))
        for name, values in entry["information"].pop("_sets").items():
            sets.setdefault(name, set()).update(values)
        per_batch.append(entry)
        store.cleanup()
        del resources, parse_issues, base, store
        gc.collect()
        print(f"batch {number}: {len(batch_names)} patients, {entry['baseline']['resources']:,} resources, "
              f"{time.perf_counter() - started:.0f} s", flush=True)
    return {"per_batch": per_batch, "sets": sets}


# --------------------------------------------------------------------------- performance series

def run_performance(sizes: list[int], repeats: int, total_patients: int, attempt_full: bool) -> dict:
    runs: list[dict] = []
    skipped: list[dict] = []
    if attempt_full and total_patients not in sizes:
        sizes = [*sizes, total_patients]
    for size in sizes:
        # Requirement from the measured scaling (about 0.6 GB base + 4 MB of commit per Synthea patient),
        # with a 25% margin, so the run never pushes the machine into heavy paging.
        needed_gb = (0.6 + size * 4.0 / 1024) * 1.25
        free = free_commit_gb()
        if free is not None and free < needed_gb:
            skipped.append({"patients": size, "needed_gb_estimate": round(needed_gb, 1), "free_commit_gb": free})
            print(f"performance: skipping {size} patients (needs ~{needed_gb:.1f} GB, {free} GB free)", flush=True)
            continue
        for repeat in range(repeats if size != total_patients else 1):
            completed = subprocess.run([sys.executable, "-m", "evaluation.perf_worker", str(size)],
                                       capture_output=True, text=True, check=False,
                                       cwd=Path(__file__).resolve().parents[1])
            if completed.returncode != 0:
                skipped.append({"patients": size, "error": completed.stderr.strip().splitlines()[-1:]})
                break
            run = json.loads(completed.stdout.strip().splitlines()[-1])
            run["repeat"] = repeat
            runs.append(run)
            print(f"performance: {size} patients, repeat {repeat}: {run['end_to_end_seconds']:.1f} s", flush=True)
    return {"runs": runs, "skipped": skipped}


# --------------------------------------------------------------------------- aggregation

def aggregate(batch_results: dict, performance: dict) -> dict:
    per_batch, sets = batch_results["per_batch"], batch_results["sets"]
    out: dict = {}

    # Dataset and baseline
    by_type: dict = {}
    issues_by_code: dict = {}
    for b in per_batch:
        _merge_counters(by_type, b["baseline"]["by_type"])
        _merge_counters(issues_by_code, b["baseline"]["issues_by_code"])
    dims: dict[str, list[int]] = {}
    for b in per_batch:
        for dim, (passed, total) in b["baseline"]["dimension_passed_total"].items():
            stats = dims.setdefault(dim, [0, 0])
            stats[0] += passed
            stats[1] += total
    out["dataset"] = {
        "patients": sum(b["patients"] for b in per_batch),
        "batches": len(per_batch),
        "bytes": sum(b["bytes"] for b in per_batch),
        "resources": sum(b["baseline"]["resources"] for b in per_batch),
        "in_scope": sum(b["baseline"]["in_scope"] for b in per_batch),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
    }
    out["baseline"] = {
        "errors": sum(b["baseline"]["errors"] for b in per_batch),
        "invalid_records": sum(b["baseline"]["invalid_records"] for b in per_batch),
        "warnings": sum(b["baseline"]["warnings"] for b in per_batch),
        "error_examples": [e for b in per_batch for e in b["baseline"]["error_examples"]][:10],
        "issues_by_code": dict(sorted(issues_by_code.items(), key=lambda kv: -kv[1])),
        "unsupported_type_issues_per_batch": [b["baseline"]["unsupported_type_issues"] for b in per_batch],
        "completeness": _ratio(sum(b["baseline"]["present_fields"] for b in per_batch),
                               sum(b["baseline"]["expected_fields"] for b in per_batch)),
        "duplicates_flagged": sum(b["baseline"]["duplicates"] for b in per_batch),
        "dimension_scores": {d: _ratio(p, t) for d, (p, t) in dims.items()},
        "parse_issues": sum(b["baseline"]["parse_issues"] for b in per_batch),
    }

    # E1 and E3: injected defects
    for key in ("validation_defects", "quality_defects"):
        per_defect: dict[str, dict] = {}
        for b in per_batch:
            for name, stats in b[key]["per_defect"].items():
                _merge_counters(per_defect.setdefault(name, {}), stats)
        injected = sum(s["injected"] for s in per_defect.values())
        flagged_invalid = sum(s.get("flagged_invalid", 0) for s in per_defect.values())
        # Records not injected in each isolated pass (so each record counts once per pass).
        clean = sum(b[key]["clean_records"] for b in per_batch)
        clean_flagged = sum(b[key]["clean_flagged_invalid"] for b in per_batch)
        entry = {
            "per_defect": {name: s | {"detection_rate": _ratio(s.get("detected", 0), s["injected"])}
                           for name, s in per_defect.items()},
            "injected": injected,
            "detected_expected_code": sum(s.get("detected", 0) for s in per_defect.values()),
            "clean_records": clean,
            "clean_flagged_invalid": clean_flagged,
        }
        entry["detection_rate"] = _ratio(entry["detected_expected_code"], injected)
        if key == "validation_defects":
            entry["record_classification"] = _prf(flagged_invalid, clean_flagged, injected - flagged_invalid,
                                                   clean - clean_flagged)
        else:
            entry["completeness_check"] = {
                "missing_fields_increase": sum(s.get("missing_field_increase", 0) for s in per_defect.values()),
                "completeness_defects_injected": sum(s["injected"] for name, s in per_defect.items()
                                                     if name in defects.COMPLETENESS_DEFECTS),
                "passes": sum(b[key]["completeness_passes"] for b in per_batch),
                "passes_matching_exactly": sum(b[key]["completeness_passes_exact"] for b in per_batch),
            }
            entry["score_change"] = {
                dim: {"before_mean": statistics.mean(b[key]["scores_before"][dim] for b in per_batch),
                      "after_mean": statistics.mean(b[key]["scores_after"][dim] for b in per_batch)}
                for dim in per_batch[0][key]["scores_before"]
            }
        out[key] = entry
    malformed = next((b["malformed_lines"] for b in per_batch if "malformed_lines" in b), None)
    out["malformed_lines"] = malformed

    # E4: duplicates
    per_case: dict[str, dict] = {}
    baseline_dups: dict = {}
    for b in per_batch:
        for name, stats in b["duplicates"]["per_case"].items():
            _merge_counters(per_case.setdefault(name, {}), stats)
        _merge_counters(baseline_dups, b["duplicates"]["baseline_flagged"])
    truth = per_batch[0]["duplicates"]["truth"]
    tp = sum(s["flagged"] for n, s in per_case.items() if truth[n])
    fn = sum(s["injected"] - s["flagged"] for n, s in per_case.items() if truth[n])
    fp = sum(s["flagged"] for n, s in per_case.items() if not truth[n])
    tn = sum(s["injected"] - s["flagged"] for n, s in per_case.items() if not truth[n])
    exact_cases = {n for n, is_dup in truth.items() if is_dup and n != "patient_new_identifiers_same_demographics"}
    tp_exact = sum(per_case[n]["flagged"] for n in exact_cases)
    fn_exact = sum(per_case[n]["injected"] - per_case[n]["flagged"] for n in exact_cases)
    out["duplicates"] = {
        "per_case": {n: s | {"is_duplicate": truth[n], "flag_rate": _ratio(s["flagged"], s["injected"])}
                     for n, s in per_case.items()},
        "all_cases": _prf(tp, fp, fn, tn),
        "excluding_fuzzy_case": _prf(tp_exact, fp, fn_exact, tn),
        "baseline_flagged": dict(sorted(baseline_dups.items(), key=lambda kv: -kv[1])),
    }

    # E2: PHI
    counts: dict[tuple, int] = {}
    for b in per_batch:
        for scope, reason, is_phi, flagged, n in b["phi"]["counts"]:
            key = (scope, reason, is_phi, flagged)
            counts[key] = counts.get(key, 0) + n
    unexpected: dict = {}
    severity: dict[tuple, int] = {}
    for b in per_batch:
        _merge_counters(unexpected, b["phi"]["unexpected_findings"])
        for sev, truth_value, n in b["phi"]["findings_by_severity_and_truth"]:
            severity[(sev, truth_value)] = severity.get((sev, truth_value), 0) + n

    def phi_metrics(selector) -> dict:
        tp = sum(n for (s, r, p, f), n in counts.items() if selector(s, r) and p and f)
        fn = sum(n for (s, r, p, f), n in counts.items() if selector(s, r) and p and not f)
        fp = sum(n for (s, r, p, f), n in counts.items() if selector(s, r) and not p and f)
        tn = sum(n for (s, r, p, f), n in counts.items() if selector(s, r) and not p and not f)
        return _prf(tp, fp, fn, tn)

    unexpected_total = sum(unexpected.values())
    overall = phi_metrics(lambda s, r: True)
    overall["fp"] += unexpected_total
    overall["precision"] = _ratio(overall["tp"], overall["tp"] + overall["fp"])
    by_reason = {}
    for (scope, reason, is_phi, flagged), n in counts.items():
        stats = by_reason.setdefault(f"{scope}: {reason}", {"is_phi": is_phi, "locations": 0, "flagged": 0})
        stats["locations"] += n
        stats["flagged"] += n if flagged else 0
    med_high_true = sum(n for (sev, t), n in severity.items() if sev in ("high", "medium") and t)
    med_high_all = sum(n for (sev, t), n in severity.items() if sev in ("high", "medium"))
    out["phi"] = {
        "overall": overall,
        "by_scope": {scope: phi_metrics(lambda s, r, scope=scope: s == scope)
                     for scope in sorted({k[0] for k in counts})},
        "by_location_type": dict(sorted(by_reason.items())),
        "unexpected_findings": unexpected,
        "precision_high_medium_only": _ratio(med_high_true, med_high_all),
        "findings_by_severity": {f"{sev}|{'phi' if t else 'not phi'}": n for (sev, t), n in sorted(severity.items())},
        "detector_seconds": sum(b["phi"]["detector_seconds"] for b in per_batch),
        "resources_scanned": sum(b["phi"]["resources_scanned"] for b in per_batch),
    }

    # E5/E6: performance
    batch_ingest = sum(b["ingest_seconds"] for b in per_batch)
    batch_pipeline = sum(b["pipeline_seconds"] for b in per_batch)
    series: dict[int, list[dict]] = {}
    for run in performance["runs"]:
        series.setdefault(run["patients"], []).append(run)
    out["performance"] = {
        "batch_mode": {
            "patients": out["dataset"]["patients"],
            "resources": out["dataset"]["resources"],
            "ingest_seconds": batch_ingest,
            "pipeline_seconds": batch_pipeline,
            "end_to_end_seconds": batch_ingest + batch_pipeline,
            "resources_per_second_end_to_end": out["dataset"]["resources"] / (batch_ingest + batch_pipeline),
            "resources_per_second_pipeline": out["dataset"]["resources"] / batch_pipeline,
            "r_status": sorted({b["r_status"] for b in per_batch}),
        },
        "single_process_series": [
            {
                "patients": size,
                "resources": runs[0]["resources"],
                "repeats": len(runs),
                "median_stage_seconds": {stage: statistics.median(r["timings"][stage] for r in runs)
                                         for stage in runs[0]["timings"]},
                "median_end_to_end_seconds": statistics.median(r["end_to_end_seconds"] for r in runs),
                "median_processing_seconds": statistics.median(r["processing_seconds"] for r in runs),
                "resources_per_second_end_to_end": runs[0]["resources"]
                / statistics.median(r["end_to_end_seconds"] for r in runs),
                "peak_commit_mb_max": max((r["memory"] or {}).get("peak_commit_mb", math.nan) for r in runs),
                "peak_working_set_mb_max": max((r["memory"] or {}).get("peak_working_set_mb", math.nan)
                                               for r in runs),
            }
            for size, runs in sorted(series.items())
        ],
        "skipped": performance["skipped"],
    }

    # E7: sanitization
    out["sanitization"] = {}
    for config in experiments.SANITIZATION_CONFIGS:
        rows = [b["sanitization"][config] for b in per_batch]
        actions, residual, checks, leaks, locations = {}, {}, {}, {}, {}
        for r in rows:
            _merge_counters(actions, r["actions"])
            _merge_counters(residual, r["residual_by_rule_severity"])
            _merge_counters(checks, r["leak_checks"])
            _merge_counters(leaks, r["leaks"])
            _merge_counters(locations, r["leak_locations"])
        out["sanitization"][config] = {
            "settings": experiments.SANITIZATION_CONFIGS[config].describe(),
            "resources_in": sum(r["resources_in"] for r in rows),
            "resources_out": sum(r["resources_out"] for r in rows),
            "seconds": sum(r["seconds"] for r in rows),
            "output_bytes": sum(r["output_bytes"] for r in rows),
            "actions": dict(sorted(actions.items())),
            "residual_findings": dict(sorted(residual.items(), key=lambda kv: -kv[1])),
            "residual_high": sum(r["residual_high"] for r in rows),
            "errors_before": sum(r["errors_before"] for r in rows),
            "errors_after": sum(r["errors_after"] for r in rows),
            "new_error_codes": sorted({c for r in rows for c in r["new_error_codes"]}),
            "reference_issues_before": sum(r["reference_issues_before"] for r in rows),
            "reference_issues_after": sum(r["reference_issues_after"] for r in rows),
            "leak_checks": checks,
            "leaks": leaks,
            "leak_locations": dict(sorted(locations.items(), key=lambda kv: -kv[1])),
            "round_trip": ({"resources_read": sum(r["round_trip"]["resources_read"] for r in rows),
                            "parse_issues": sum(r["round_trip"]["parse_issues"] for r in rows)}
                           if rows[0]["round_trip"] else None),
        }

    # E8: information preserved
    info = [b["information"] for b in per_batch]

    def pooled(key: str) -> dict:
        total: dict = {}
        for i in info:
            _merge_counters(total, i[key])
        return total

    def pooled_moments(key: str) -> dict[str, tuple[int, float, float]]:
        total: dict[str, list[float]] = {}
        for i in info:
            for series_key, (n, s, ss) in i[key].items():
                t = total.setdefault(series_key, [0, 0.0, 0.0])
                t[0] += n
                t[1] += s
                t[2] += ss
        result = {}
        for series_key, (n, s, ss) in total.items():
            mean = s / n
            sd = math.sqrt(max(ss / n - mean * mean, 0.0) * n / (n - 1)) if n > 1 else 0.0
            result[series_key] = (n, mean, sd)
        return result

    raw_q, analytic_q = pooled_moments("quantity_sum_raw"), pooled_moments("quantity_sum_analytic")
    patients_raw = sum(i["patients"][0] for i in info)
    patients_analytic = sum(i["patients"][1] for i in info)
    prev_raw, prev_an = pooled("condition_patients_raw"), pooled("condition_patients_analytic")
    prevalence_diff = max((abs(100 * prev_raw.get(c, 0) / patients_raw - 100 * prev_an.get(c, 0) / patients_analytic)
                           for c in set(prev_raw) | set(prev_an)), default=0.0)
    obs_raw_months, obs_an_months = pooled("observation_months_raw"), pooled("observation_months_analytic")
    cond_raw_months, cond_an_months = pooled("condition_months_raw"), pooled("condition_months_analytic")
    age_raw, age_an = pooled("age_group_raw"), pooled("age_group_analytic")
    out["information"] = {
        "records_raw_vs_analytic": {k: [sum(i[k][0] for i in info), sum(i[k][1] for i in info)]
                                    for k in ("patients", "conditions", "observations")},
        "gender_identical": pooled("gender_raw") == pooled("gender_analytic"),
        "gender_raw": pooled("gender_raw"),
        "age_group_raw_exact": age_raw,
        "age_group_analytic": age_an,
        "age_group_agreement": _ratio(sum(i["age_group_agreement"] for i in info), patients_raw),
        "age_group_distribution_tvd": 0.5 * sum(abs(age_raw.get(g, 0) - age_an.get(g, 0))
                                                for g in set(age_raw) | set(age_an)) / patients_raw,
        "state_identical": pooled("state_raw") == pooled("state_analytic"),
        "distinct_cities_raw": len(sets.get("cities", ())),
        "distinct_postal_codes_raw": len(sets.get("postal_codes", ())),
        "distinct_observation_days_raw": len(sets.get("observation_days", ())),
        "distinct_observation_months_analytic": len(sets.get("analytic_observation_periods", ())),
        "condition_codes": [len(prev_raw), len(prev_an)],
        "max_prevalence_difference_pct_points": prevalence_diff,
        "observation_series": [len(raw_q), len(analytic_q)],
        "observation_series_same_n": sum(raw_q[k][0] == analytic_q.get(k, (None,))[0] for k in raw_q),
        "max_abs_mean_difference": max((abs(raw_q[k][1] - analytic_q[k][1]) for k in raw_q if k in analytic_q),
                                       default=None),
        "max_abs_sd_difference": max((abs(raw_q[k][2] - analytic_q[k][2]) for k in raw_q if k in analytic_q),
                                     default=None),
        "observation_months_identical": obs_raw_months == obs_an_months,
        "condition_months_identical": cond_raw_months == cond_an_months,
        "observation_months": len(obs_raw_months),
        "linked_conditions": [sum(i["linked_conditions_raw"] for i in info),
                              sum(i["linked_conditions_analytic"] for i in info)],
        "linked_observations": [sum(i["linked_observations_raw"] for i in info),
                                sum(i["linked_observations_analytic"] for i in info)],
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--patients", type=int, default=None, help="limit to the first N patients")
    parser.add_argument("--batch-size", type=int, default=150)
    parser.add_argument("--perf-sizes", default="50,150,300,600")
    parser.add_argument("--perf-repeats", type=int, default=3)
    parser.add_argument("--no-full-perf", action="store_true", help="don't attempt all patients in one process")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    env = environment()
    archive = zipfile.ZipFile(ensure_dataset())
    names = patient_files(archive)[: args.patients]
    print(f"{len(names)} patient files, batches of {args.batch_size}", flush=True)

    batch_results = run_batches(archive, names, args.batch_size)
    sizes = [int(s) for s in args.perf_sizes.split(",") if s and int(s) <= len(names)]
    performance = run_performance(sizes, args.perf_repeats, len(names), attempt_full=not args.no_full_perf)
    results = {
        "meta": {
            "started": started.isoformat(timespec="seconds"),
            "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reference_date": experiments.EVAL_NOW.date().isoformat(),
            "seed": experiments.SEED,
            "batch_size": args.batch_size,
            "per_defect_per_batch": experiments.PER_DEFECT,
            "per_duplicate_case_per_batch": experiments.PER_DUPLICATE_CASE,
            "data_url": DATA_URL,
            "data_sha256": DATA_SHA256,
            "environment": env,
            "orchestrator_peak_memory": peak_memory_mb(),
        },
        **aggregate(batch_results, performance),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_bytes(to_json(results))  # NaN/inf become null: always valid JSON
    (args.out / "results.md").write_text(render_markdown(results), encoding="utf-8")
    print(f"wrote {args.out / 'results.json'} and results.md")


if __name__ == "__main__":
    main()
