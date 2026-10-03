from __future__ import annotations

import pytest

from conftest import FIXED_NOW, good_condition, good_observation, good_patient, validate, without
from healthdata_insight.analysis import (
    analyze_store,
    completeness_frame,
    issue_breakdown_frame,
    issues_frame,
)
from healthdata_insight.ingest import ingest_file
from healthdata_insight.models import Dimension, IssueCode, QualityGrade
from healthdata_insight.quality import assess_quality, grade_for
from healthdata_insight.samples import load_samples


def test_perfect_dataset():
    quality = assess_quality(validate(good_patient(), good_condition(), good_observation()))
    assert [d.percent for d in quality.dimensions] == [100.0, 100.0, 100.0, 100.0]
    assert quality.overall_percent == 100.0
    assert quality.grade is QualityGrade.EXCELLENT
    assert (quality.missing_field_count, quality.duplicate_count, quality.invalid_record_count) == (0, 0, 0)
    assert quality.summary == "Excellent overall data quality (100%). No issues found."


def test_completeness_counts_expected_fields():
    # Patient has 5 expected fields; this one has only id -> 1/5 present.
    quality = assess_quality(validate({"resourceType": "Patient", "id": "p1"}))
    assert (quality.completeness.passed, quality.completeness.total) == (1, 5)
    assert quality.completeness.percent == 20.0
    assert quality.missing_field_count == 4


def test_validity_includes_malformed_records():
    report = validate(good_patient(), good_patient("p2", gender="M"))
    assert assess_quality(report).validity.percent == 50.0


def test_consistency_counts_resources_not_issues():
    report = validate(
        good_patient(),
        # Two consistency issues on one resource still count as one inconsistent resource.
        good_condition(abatementDateTime="2005-01-01"),
        good_observation(),
    )
    consistency = assess_quality(report).consistency
    assert (consistency.passed, consistency.total) == (2, 3)


def test_uniqueness_and_duplicate_count():
    quality = assess_quality(validate(good_patient(), good_patient(), good_patient("p2", gender="male")))
    assert quality.duplicate_count == 1
    assert (quality.uniqueness.passed, quality.uniqueness.total) == (2, 3)


def test_out_of_scope_only_dataset_is_not_rated():
    quality = assess_quality(validate({"resourceType": "Encounter", "id": "e1"}))
    assert quality.overall_percent is None
    assert quality.grade is QualityGrade.NOT_RATED
    assert quality.completeness.percent is None


@pytest.mark.parametrize("percent, grade", [
    (100, QualityGrade.EXCELLENT), (95, QualityGrade.EXCELLENT), (94.9, QualityGrade.GOOD),
    (85, QualityGrade.GOOD), (70, QualityGrade.FAIR), (69.9, QualityGrade.POOR), (None, QualityGrade.NOT_RATED),
])
def test_grades(percent, grade):
    assert grade_for(percent) is grade


def test_highlights_rank_by_affected_records_and_skip_informational_codes():
    report = validate(
        good_patient(),
        without(good_observation("o1"), "valueQuantity"),
        without(good_observation("o2", effectiveDateTime="2023-02-01T00:00:00Z"), "valueQuantity"),
        good_condition(onsetDateTime="not-a-date"),
        {"resourceType": "Encounter", "id": "e1"},
    )
    quality = assess_quality(report)
    assert quality.highlights[0].code is IssueCode.MISSING_VALUE
    assert quality.highlights[0].records == 2
    assert IssueCode.UNSUPPORTED_RESOURCE_TYPE not in {h.code for h in quality.highlights}
    assert "observation has no value (2)" in quality.summary


def test_corrupted_sample_scores(store):
    for name, data in load_samples("Corrupted synthetic data"):
        ingest_file(store, name, data)
    analysis = analyze_store(store, now=FIXED_NOW)
    quality = analysis.quality

    assert quality.validity.percent == pytest.approx(100 * 9 / 26)
    assert quality.duplicate_count == 4
    assert quality.invalid_record_count == 17
    assert quality.missing_field_count == 13
    assert quality.grade in (QualityGrade.FAIR, QualityGrade.POOR)
    assert all(d.percent < 100 for d in quality.dimensions)

    clean = analyze_store_for(store, "Clean synthetic data")
    assert clean.overall_percent > quality.overall_percent


def analyze_store_for(store, sample_set):
    store.retain_only([])
    for name, data in load_samples(sample_set):
        ingest_file(store, name, data)
    return analyze_store(store, now=FIXED_NOW).quality


def test_frames():
    report = validate(good_patient(), {"resourceType": "Observation", "id": "o1"}, good_patient())

    issues = issues_frame(report.issues)
    assert len(issues) == len(report.issues)
    assert set(issues["Dimension"]) <= {d.value for d in Dimension}
    assert "Observation/o1" in set(issues["Resource"])

    breakdown = issue_breakdown_frame(report.issues)
    assert breakdown["Count"].sum() == len(report.issues)
    assert breakdown.iloc[0]["Severity"] == "error"  # errors listed first

    completeness = completeness_frame(report)
    assert set(completeness["Resource type"]) == {"Patient", "Observation"}  # no Conditions -> no rows
    assert completeness["Missing"].sum() == assess_quality(report).missing_field_count

    assert issue_breakdown_frame(()).empty
    assert issues_frame(()).empty
