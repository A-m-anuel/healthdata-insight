"""Dataset summary statistics and table builders for the UI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pandas as pd

from healthdata_insight.config import IN_SCOPE_RESOURCE_TYPES
from healthdata_insight.storage import DatasetStore, FileRecord


@dataclass(frozen=True)
class DatasetSummary:
    file_count: int
    total_bytes: int
    total_resources: int
    bundle_count: int
    parse_issue_count: int
    distinct_patients: int
    resource_counts: dict[str, int]

    @property
    def resource_type_count(self) -> int:
        return len(self.resource_counts)

    @property
    def in_scope_counts(self) -> dict[str, int]:
        return {t: self.resource_counts.get(t, 0) for t in IN_SCOPE_RESOURCE_TYPES}

    @property
    def out_of_scope_types(self) -> list[str]:
        return [t for t in self.resource_counts if t not in IN_SCOPE_RESOURCE_TYPES]


def build_summary(
    files: Sequence[FileRecord],
    resource_counts: Mapping[str, int],
    distinct_patients: int = 0,
) -> DatasetSummary:
    ordered = dict(sorted(resource_counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return DatasetSummary(
        file_count=len(files),
        total_bytes=sum(f.size_bytes for f in files),
        total_resources=sum(ordered.values()),
        bundle_count=sum(f.bundle_count for f in files),
        parse_issue_count=sum(f.parse_issue_count for f in files),
        distinct_patients=distinct_patients,
        resource_counts=ordered,
    )


def summarize_store(store: DatasetStore) -> DatasetSummary:
    return build_summary(store.files(), store.resource_counts(), store.distinct_resource_ids("Patient"))


def resource_counts_frame(summary: DatasetSummary) -> pd.DataFrame:
    rows = [
        {
            "Resource type": resource_type,
            "Count": count,
            "Share (%)": 100 * count / summary.total_resources if summary.total_resources else 0.0,
            "In scope": resource_type in IN_SCOPE_RESOURCE_TYPES,
        }
        for resource_type, count in summary.resource_counts.items()
    ]
    return pd.DataFrame(rows, columns=["Resource type", "Count", "Share (%)", "In scope"])


def files_frame(files: Sequence[FileRecord]) -> pd.DataFrame:
    rows = [
        {
            "File": f.name,
            "Format": f.format,
            "Size": format_bytes(f.size_bytes),
            "Resources": f.resource_count,
            "Bundles": f.bundle_count,
            "Parse issues": f.parse_issue_count,
        }
        for f in files
    ]
    return pd.DataFrame(rows, columns=["File", "Format", "Size", "Resources", "Bundles", "Parse issues"])


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024**2:.1f} MB"
