"""Time each processing stage on the first N patients, in this (fresh) process, and report peak memory.

Usage: python -m evaluation.perf_worker <n_patients>    (prints one JSON object)

The stages are the same function calls, in the same order, as ``pipeline.process_dataset``; they are
invoked individually here only so each one can be timed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from healthdata_insight.analytics import build_analytical_dataset
from healthdata_insight.persistence import RunRepository
from healthdata_insight.phi import detect_phi
from healthdata_insight.quality import assess_quality
from healthdata_insight.r_charts import run_r_analysis
from healthdata_insight.summary import summarize_store
from healthdata_insight.validation import validate_dataset

from evaluation.data import DATA_PATH, load_batch, patient_files
from evaluation.experiments import EVAL_NOW
from evaluation.sysinfo import peak_memory_mb


def main(n_patients: int) -> dict:
    archive = zipfile.ZipFile(DATA_PATH)
    names = patient_files(archive)[:n_patients]
    loaded = load_batch(archive, names)
    store = loaded.store
    timings = {"ingest": loaded.ingest_seconds}

    def timed(stage, fn, *args, **kwargs):
        started = time.perf_counter()
        value = fn(*args, **kwargs)
        timings[stage] = time.perf_counter() - started
        return value

    resources = timed("load", store.resources)
    validation = timed("validate", validate_dataset, resources, store.parse_issues(), now=EVAL_NOW)
    quality = timed("quality", assess_quality, validation)
    summary = timed("summary", summarize_store, store)
    privacy = timed("phi_scan", detect_phi, resources, now=EVAL_NOW)
    analytics = timed("analytics", build_analytical_dataset, resources, validation, now=EVAL_NOW)
    r_output = timed("r_analysis", run_r_analysis, analytics)
    with tempfile.TemporaryDirectory() as tmp:
        repo = RunRepository(Path(tmp) / "history.db")
        timed("persist", repo.record_run, store.files(), summary, validation, quality, privacy,
              analytical_records=analytics.record_count, r_status=r_output.status.value)
    store.cleanup()

    processing = sum(v for k, v in timings.items() if k != "ingest")
    return {
        "patients": len(names),
        "files_bytes": loaded.bytes,
        "resources": len(resources),
        "in_scope_resources": validation.in_scope_count,
        "timings": timings,
        "processing_seconds": processing,
        "end_to_end_seconds": processing + timings["ingest"],
        "r_status": r_output.status.value,
        "memory": peak_memory_mb(),
    }


if __name__ == "__main__":
    print(json.dumps(main(int(sys.argv[1]))))
