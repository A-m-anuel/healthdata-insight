"""The processing step: everything that happens when the user clicks "Process dataset".

Kept free of Streamlit so the whole workflow can be run and tested headlessly:
analysis (summary, validation, quality, PHI scan) -> pseudonymized analytical
dataset -> optional R analysis -> persisted processing run.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from healthdata_insight.analysis import DatasetAnalysis, analyze_store
from healthdata_insight.analytics import AnalyticalDataset, build_analytical_dataset
from healthdata_insight.persistence import RunRepository
from healthdata_insight.r_charts import ROutput, RStatus, run_r_analysis
from healthdata_insight.storage import DatasetStore, FileRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessingResult:
    fingerprint: tuple[str, ...]  # SHA-256 of each input file, in order
    files: tuple[FileRecord, ...]
    processed_at: datetime
    duration_ms: int
    analysis: DatasetAnalysis
    analytics: AnalyticalDataset
    r_output: ROutput
    run_id: int | None  # None if the run could not be persisted
    persistence_error: str | None = None


def dataset_fingerprint(store: DatasetStore) -> tuple[str, ...]:
    return tuple(f.sha256 for f in store.files())


def process_dataset(
    store: DatasetStore,
    repo: RunRepository | None,
    *,
    use_r: bool = True,
    now: datetime | None = None,
) -> ProcessingResult:
    started = time.perf_counter()
    processed_at = now or datetime.now(timezone.utc)
    files = store.files()
    resources = store.resources()

    analysis = analyze_store(store, resources=resources, now=processed_at)
    analytics = build_analytical_dataset(resources, analysis.validation, now=processed_at)
    r_output = run_r_analysis(analytics) if use_r else ROutput(RStatus.UNAVAILABLE, "R analysis is turned off.")
    duration_ms = round((time.perf_counter() - started) * 1000)

    run_id, error = None, None
    if repo is not None:
        try:
            run_id = repo.record_run(
                files, analysis.summary, analysis.validation, analysis.quality, analysis.privacy,
                duration_ms=duration_ms, analytical_records=analytics.record_count, r_status=r_output.status.value,
            )
        except (sqlite3.Error, OSError) as exc:
            logger.exception("Could not persist the processing run")
            error = str(exc)

    return ProcessingResult(
        fingerprint=tuple(f.sha256 for f in files),
        files=tuple(files),
        processed_at=processed_at,
        duration_ms=duration_ms,
        analysis=analysis,
        analytics=analytics,
        r_output=r_output,
        run_id=run_id,
        persistence_error=error,
    )
