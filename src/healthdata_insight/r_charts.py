"""Runs the R analysis script (``r/analytics_plots.R``) on the analytical dataset.

R receives only the pseudonymized analytical extracts as CSV files in a
private temporary directory, which is deleted afterwards. R is optional: if
``Rscript`` can't be found, or the script fails or times out, the result
says so and the dashboard falls back to its Python charts.

Rscript is located via the ``HDI_RSCRIPT`` environment variable, then
``PATH``, then the standard Windows install folders.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from importlib.resources import as_file, files
from pathlib import Path

import pandas as pd

from healthdata_insight.analytics import AnalyticalDataset

RSCRIPT_ENV = "HDI_RSCRIPT"
DEFAULT_TIMEOUT_SECONDS = 120
_WINDOWS_GLOBS = (r"C:\Program Files\R\R-*\bin\Rscript.exe", r"C:\Program Files\R\R-*\bin\x64\Rscript.exe")


class RStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class RChart:
    name: str
    title: str
    png: bytes


@dataclass(frozen=True)
class ROutput:
    status: RStatus
    message: str = ""
    r_version: str | None = None
    charts: tuple[RChart, ...] = ()
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    notes: tuple[str, ...] = ()  # plots R skipped or could not draw, with the reason


def find_rscript() -> Path | None:
    configured = os.environ.get(RSCRIPT_ENV)
    if configured:
        return Path(configured) if Path(configured).is_file() else None
    on_path = shutil.which("Rscript")
    if on_path:
        return Path(on_path)
    for pattern in _WINDOWS_GLOBS:
        matches = sorted(glob.glob(pattern), reverse=True)  # newest version first
        if matches:
            return Path(matches[0])
    return None


def run_r_analysis(
    dataset: AnalyticalDataset,
    *,
    rscript: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ROutput:
    rscript = rscript or find_rscript()
    if rscript is None:
        return ROutput(RStatus.UNAVAILABLE, "R is not installed (Rscript not found). "
                                            f"Install R or set {RSCRIPT_ENV} to enable R visualizations.")

    work = Path(tempfile.mkdtemp(prefix="healthdata_insight_r_"))
    try:
        in_dir, out_dir = work / "in", work / "out"
        in_dir.mkdir()
        dataset.patients.to_csv(in_dir / "patients.csv", index=False)
        dataset.conditions.to_csv(in_dir / "conditions.csv", index=False)
        dataset.observations.to_csv(in_dir / "observations.csv", index=False)

        with as_file(files("healthdata_insight") / "r" / "analytics_plots.R") as script:
            try:
                completed = subprocess.run(
                    [str(rscript), "--vanilla", str(script), str(in_dir), str(out_dir)],
                    capture_output=True, text=True, timeout=timeout, check=False,
                )
            except subprocess.TimeoutExpired:
                return ROutput(RStatus.FAILED, f"R analysis timed out after {timeout:.0f} s.")
            except OSError as exc:
                return ROutput(RStatus.FAILED, f"Could not start R: {exc}")
        if completed.returncode != 0 or not (out_dir / "manifest.csv").exists():
            detail = (completed.stderr or completed.stdout).strip().splitlines()[-3:]
            return ROutput(RStatus.FAILED, "R analysis failed: " + " ".join(detail))
        return _collect(out_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _collect(out_dir: Path) -> ROutput:
    manifest = pd.read_csv(out_dir / "manifest.csv", keep_default_na=False)
    charts, tables, notes = [], {}, []
    for row in manifest.itertuples():
        if row.kind == "plot":
            charts.append(RChart(row.name, row.title, (out_dir / row.file).read_bytes()))
        elif row.kind == "table":
            tables[row.name] = pd.read_csv(out_dir / row.file)
        else:
            notes.append(f"{row.title}: {row.note}")
    version_file = out_dir / "r_version.txt"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else None
    return ROutput(RStatus.OK, r_version=version, charts=tuple(charts), tables=tables, notes=tuple(notes))
