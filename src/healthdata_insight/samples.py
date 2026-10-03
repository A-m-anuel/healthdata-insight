"""Access to the bundled synthetic sample files (see scripts/generate_samples.py)."""

from __future__ import annotations

from importlib.resources import files

SAMPLE_SETS: dict[str, tuple[str, ...]] = {
    "Clean synthetic data": ("synthetic_bundle.json", "synthetic_resources.ndjson"),
    "Corrupted synthetic data": ("synthetic_corrupted.ndjson",),
}


def load_samples(sample_set: str = "Clean synthetic data") -> list[tuple[str, bytes]]:
    """Return ``(file name, raw bytes)`` for each file in a bundled sample set."""
    directory = files("healthdata_insight") / "sample_data"
    return [(name, (directory / name).read_bytes()) for name in SAMPLE_SETS[sample_set]]
