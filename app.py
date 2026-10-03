"""HealthData Insight - Streamlit entry point.

Run with:  streamlit run app.py

Workflow: upload FHIR R4 files -> process the dataset -> view the analysis ->
explore the pseudonymized analytical data -> download reports. The page logic lives in
``healthdata_insight.ui``; everything below the UI is plain, tested Python.
"""

from __future__ import annotations

import logging
import sqlite3

import streamlit as st

from healthdata_insight import __version__
from healthdata_insight.config import APP_NAME, FHIR_VERSION, IN_SCOPE_RESOURCE_TYPES, MAX_FILE_SIZE_MB
from healthdata_insight.ingest import IngestResult, IngestStatus, ingest_file
from healthdata_insight.parser import file_sha256
from healthdata_insight.persistence import RunRepository
from healthdata_insight.pipeline import ProcessingResult, dataset_fingerprint, process_dataset
from healthdata_insight.r_charts import find_rscript
from healthdata_insight.samples import SAMPLE_SETS, load_samples
from healthdata_insight.storage import DatasetStore
from healthdata_insight.summary import format_bytes
from healthdata_insight.ui.audit import render_audit
from healthdata_insight.ui.common import log_event, md_escape
from healthdata_insight.ui.downloads import render_downloads
from healthdata_insight.ui.explorer import render_explorer
from healthdata_insight.ui.overview import render_overview, render_quality, render_validation
from healthdata_insight.ui.privacy import render_privacy
from healthdata_insight.ui.public_health import render_public_health

logger = logging.getLogger(__name__)

NO_SAMPLE = "None"
SECTIONS = ["Overview", "Data quality", "FHIR validation", "Privacy", "Public health", "Data explorer", "Audit",
            "Reports"]
WORKFLOW = [
    ("Upload", "Add FHIR R4 JSON or NDJSON files in the sidebar, or pick a synthetic sample."),
    ("Process", "Validate, score quality, scan for PHI, and build the pseudonymized analytical dataset."),
    ("View analysis", "Review the overview, data quality, FHIR validation, and privacy findings."),
    ("Explore", "Analyze public-health patterns and filter the pseudonymized analytical data."),
    ("Download", "Export reports and an optional sanitized copy of the dataset."),
]

st.set_page_config(page_title=APP_NAME, page_icon="🩺", layout="wide")
st.markdown("<style>.block-container {padding-top: 2.2rem;}</style>", unsafe_allow_html=True)


# --------------------------------------------------------------------------- state

def init_state() -> None:
    state = st.session_state
    if "store" not in state:
        state.store = DatasetStore.create_temporary()
    if "repo" not in state:
        try:
            state.repo = RunRepository()
        except (sqlite3.Error, OSError):
            logger.exception("Could not open the history database")
            state.repo = None
    state.setdefault("uploader_key", 0)
    state.setdefault("sample_set", NO_SAMPLE)
    state.setdefault("failures", {})  # sha256 -> IngestResult, so bad files aren't re-parsed every rerun
    state.setdefault("result", None)  # ProcessingResult of the last processed dataset
    state.setdefault("sanitized", None)  # (fingerprint, SanitizationResult)


def clear_dataset() -> None:
    state = st.session_state
    log_event("dataset.cleared")
    state.uploader_key += 1  # a new key resets the file uploader widget
    state.sample_set = NO_SAMPLE
    state.failures = {}
    state.store.retain_only([])
    state.result = None
    state.sanitized = None


# --------------------------------------------------------------------------- ingestion

def ingest_sources(store: DatasetStore, sources: list[tuple[str, bytes]]) -> list[IngestResult]:
    """Bring the store in line with ``sources``; return results for files that need reporting."""
    failures: dict[str, IngestResult] = st.session_state.failures
    active: set[str] = set()
    reported: list[IngestResult] = []

    for name, data in sources:
        sha256 = file_sha256(data)
        if sha256 in active:
            reported.append(IngestResult(name, sha256, IngestStatus.ALREADY_LOADED,
                                         "Identical to another uploaded file; skipped."))
            continue
        if sha256 in failures:
            reported.append(failures[sha256])
            continue
        try:
            result = ingest_file(store, name, data)
        except Exception:  # noqa: BLE001 - surface any unexpected failure to the user
            # Log the hash, not the file name: file names can contain patient names.
            logger.exception("Unexpected error while reading file sha256=%s", sha256)
            result = IngestResult(name, sha256, IngestStatus.FAILED,
                                  "An unexpected error occurred while reading this file.")

        if result.status is IngestStatus.FAILED:
            failures[sha256] = result
            reported.append(result)
            # Hash and size only: the file name and error text may contain identifying details.
            log_event("file.rejected", {"sha256": sha256, "size_bytes": len(data)})
        else:
            active.add(sha256)

    # Files removed from the uploader are removed from the dataset too.
    store.retain_only(active)
    return reported


def show_ingest_messages(results: list[IngestResult]) -> None:
    for result in results:
        if result.status is IngestStatus.ALREADY_LOADED:
            st.info(f"**{md_escape(result.name)}**: {result.message}", icon="ℹ️")
        else:
            st.error(f"**{md_escape(result.name)}** was rejected: {md_escape(result.message)}", icon="🚫")
            if result.issues:
                with st.expander(f"Details for {md_escape(result.name)}"):
                    for issue in result.issues[:50]:
                        st.text(str(issue))


# --------------------------------------------------------------------------- sidebar

def render_sidebar_inputs() -> list:
    with st.sidebar:
        st.markdown(f"## 🩺 {APP_NAME}")
        st.caption(f"FHIR {FHIR_VERSION} data quality, privacy, and public-health analytics · v{__version__}")
        st.markdown("**1 · Upload**")
        uploaded = st.file_uploader(
            "FHIR R4 files", type=["json", "ndjson", "jsonl"], accept_multiple_files=True,
            key=f"uploader_{st.session_state.uploader_key}",
            help=f"JSON (resource, array, or Bundle) or NDJSON, up to {MAX_FILE_SIZE_MB} MB per file.",
        )
        st.selectbox("Or use sample data", [NO_SAMPLE, *SAMPLE_SETS], key="sample_set",
                     help="Fictional FHIR R4 data. The corrupted set contains deliberate errors.")
    return uploaded or []


def render_sidebar_actions(store: DatasetStore) -> bool:
    files = store.files()
    with st.sidebar:
        st.markdown("**2 · Process**")
        clicked = st.button("Process dataset", type="primary", disabled=not files, width="stretch")
        status = st.empty()
        st.button("Clear dataset", on_click=clear_dataset, width="stretch")
        st.divider()
        rscript = find_rscript()
        st.caption(f"In scope: {', '.join(IN_SCOPE_RESOURCE_TYPES)}  \n"
                   f"R analysis: {'available' if rscript else 'not installed'}")
        st.warning("Use only synthetic or authorized data. Results support review; they are not a HIPAA "
                   "compliance determination.", icon="⚠️")
    st.session_state.sidebar_status = status
    return clicked


def update_sidebar_status(store: DatasetStore, result: ProcessingResult | None) -> None:
    files = store.files()
    status = st.session_state.sidebar_status
    if not files:
        status.caption("Add at least one file to begin.")
    elif result is None:
        status.caption(f"{len(files)} file(s) ready · {format_bytes(sum(f.size_bytes for f in files))}")
    elif result.fingerprint != dataset_fingerprint(store):
        status.caption("⚠️ Files changed since processing. Process again to update.")
    else:
        status.caption(f"✓ Processed {result.processed_at:%H:%M:%S} UTC"
                       + (f" · run #{result.run_id}" if result.run_id is not None else ""))


# --------------------------------------------------------------------------- pages

def render_welcome(store: DatasetStore) -> None:
    st.title(APP_NAME)
    st.markdown("Validate FHIR R4 healthcare data, measure its quality, review privacy risks, and analyze "
                "public-health patterns in a pseudonymized, generalized view of the data.")
    if store.files():
        st.info(f"{len(store.files())} file(s) loaded. Click **Process dataset** in the sidebar to analyze them.",
                icon="▶️")
    else:
        st.info("Upload FHIR R4 files in the sidebar, or choose sample data, to begin.", icon="📄")
    cols = st.columns(len(WORKFLOW))
    for i, (col, (step, text)) in enumerate(zip(cols, WORKFLOW), start=1):
        with col.container(border=True, height="stretch"):
            st.markdown(f"**{i} · {step}**")
            st.caption(text)


def render_dashboard(result: ProcessingResult, store: DatasetStore, rejected_files: int) -> None:
    summary = result.analysis.summary
    st.title(APP_NAME)
    st.caption(
        (f"Run #{result.run_id} · " if result.run_id is not None else "")
        + f"Processed {result.processed_at:%Y-%m-%d %H:%M} UTC · {summary.file_count} file(s) · "
        f"{summary.total_resources:,} resources · {result.duration_ms:,} ms"
    )
    if result.fingerprint != dataset_fingerprint(store):
        st.warning("The files changed after this analysis was produced. Click **Process dataset** to update it.",
                   icon="⚠️")
    if result.persistence_error:
        st.warning("This run could not be saved to the history database.", icon="⚠️")

    tabs = st.tabs(SECTIONS)
    with tabs[0]:
        render_overview(result)
    with tabs[1]:
        render_quality(result)
    with tabs[2]:
        render_validation(result, rejected_files)
    with tabs[3]:
        render_privacy(result, store)
    with tabs[4]:
        render_public_health(result)
    with tabs[5]:
        render_explorer(result)
    with tabs[6]:
        render_audit(result)
    with tabs[7]:
        render_downloads(result, store)


def main() -> None:
    init_state()
    store: DatasetStore = st.session_state.store

    uploaded = render_sidebar_inputs()
    sources = [(f.name, f.getvalue()) for f in uploaded]
    if st.session_state.sample_set != NO_SAMPLE:
        sources += load_samples(st.session_state.sample_set)
    ingest_results = ingest_sources(store, sources)

    if render_sidebar_actions(store):
        try:
            with st.spinner("Processing: validating, scoring quality, scanning for PHI, and building analytics…"):
                st.session_state.result = process_dataset(store, st.session_state.repo)
            st.session_state.sanitized = None
        except Exception:  # noqa: BLE001 - keep the app usable; details go to the server log only
            logger.exception("Processing failed")
            st.error("Processing failed because of an unexpected error. Previous results (if any) are kept; "
                     "details are in the server log.", icon="🚫")
    result = st.session_state.result
    update_sidebar_status(store, result)

    show_ingest_messages(ingest_results)
    if result is None:
        render_welcome(store)
    else:
        rejected = sum(r.status is IngestStatus.FAILED for r in ingest_results)
        render_dashboard(result, store, rejected)


main()
