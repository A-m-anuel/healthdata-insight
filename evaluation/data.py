"""The evaluation dataset: Synthea's published "1K Sample Synthetic Patient Records, FHIR R4".

The archive is downloaded once into ``evaluation/data/`` (git-ignored) and verified by SHA-256, so every
run uses byte-identical input. Patients are processed in sorted file-name order, in fixed-size batches.
"""

from __future__ import annotations

import hashlib
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from healthdata_insight.ingest import IngestStatus, ingest_file
from healthdata_insight.storage import DatasetStore

DATA_URL = ("https://synthetichealth.github.io/synthea-sample-data/downloads/"
            "synthea_sample_data_fhir_r4_sep2019.zip")
# Recorded when the archive was first downloaded for this evaluation (2026-10-03).
DATA_SHA256 = "a6fc595d9c0f4c646746af42f861b5a12d03c856af158dd837c764dfb81b66f8"
DATA_PATH = Path(__file__).resolve().parent / "data" / "synthea_sample_data_fhir_r4_sep2019.zip"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dataset(path: Path = DATA_PATH) -> Path:
    """Download the archive if needed and verify its checksum."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {DATA_URL} ...")
        partial = path.with_suffix(".part")
        urllib.request.urlretrieve(DATA_URL, partial)  # noqa: S310 - fixed https URL
        partial.replace(path)
    actual = sha256_of(path)
    if actual != DATA_SHA256:
        raise RuntimeError(f"Checksum mismatch for {path}: expected {DATA_SHA256}, got {actual}. "
                           "Delete the file to download it again.")
    return path


def patient_files(archive: zipfile.ZipFile) -> list[str]:
    return sorted(info.filename for info in archive.infolist() if not info.is_dir())


def batches(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


@dataclass
class LoadedBatch:
    store: DatasetStore
    files: int
    bytes: int
    ingest_seconds: float


def load_batch(archive: zipfile.ZipFile, names: list[str]) -> LoadedBatch:
    """Ingest files through the application's own ingestion path (parser + temporary store)."""
    store = DatasetStore.create_temporary()
    total = 0
    started = time.perf_counter()
    for name in names:
        data = archive.read(name)
        total += len(data)
        # Synthea file names contain the synthetic patient's name; keep only the UUID at the end.
        result = ingest_file(store, f"patient_{Path(name).stem[-36:]}.json", data)
        if result.status is IngestStatus.FAILED:
            raise RuntimeError(f"{name}: {result.message}")
    return LoadedBatch(store, len(names), total, time.perf_counter() - started)
