from __future__ import annotations

from datetime import datetime, timezone

import pytest

from healthdata_insight.fhir import R4_RESOURCE_TYPES, is_valid_id, parse_fhir_datetime, parse_reference


class TestIds:
    @pytest.mark.parametrize("value", ["p1", "abc-123.x", "A" * 64, "0"])
    def test_valid(self, value):
        assert is_valid_id(value)

    @pytest.mark.parametrize("value", ["", "has space", "bad!", "A" * 65, "under_score", 123, None])
    def test_invalid(self, value):
        assert not is_valid_id(value)


class TestDates:
    @pytest.mark.parametrize("value, start, end", [
        ("2020", datetime(2020, 1, 1), datetime(2021, 1, 1)),
        ("2020-02", datetime(2020, 2, 1), datetime(2020, 3, 1)),
        ("2020-02-29", datetime(2020, 2, 29), datetime(2020, 3, 1)),
    ])
    def test_partial_dates_cover_a_range(self, value, start, end):
        parsed = parse_fhir_datetime(value, date_only=True)
        assert parsed.start == start.replace(tzinfo=timezone.utc)
        assert parsed.end == end.replace(tzinfo=timezone.utc)

    def test_datetime_with_offset_is_normalised_to_utc(self):
        parsed = parse_fhir_datetime("2020-06-01T10:30:00+02:00")
        assert parsed.start == datetime(2020, 6, 1, 8, 30, tzinfo=timezone.utc)

    @pytest.mark.parametrize("value", [
        "2021-02-29",            # not a leap year
        "1985-13-40",            # month and day out of range
        "0000-01-01",            # year zero
        "2020-1-5",              # missing zero padding
        "05/12/1980",            # wrong format
        "2020-06-01T10:30:00",   # time without a time zone
        "2020-06-01T25:00:00Z",  # hour out of range
        "yesterday",
        "",
        19800512,
        None,
    ])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            parse_fhir_datetime(value)

    def test_date_only_rejects_time(self):
        with pytest.raises(ValueError):
            parse_fhir_datetime("2020-06-01T10:30:00Z", date_only=True)

    def test_definitely_before_respects_precision(self):
        year = parse_fhir_datetime("1980")
        day = parse_fhir_datetime("1980-05-12")
        assert not year.definitely_before(day)  # "1980" may be after May 12
        assert parse_fhir_datetime("1979").definitely_before(day)


class TestReferences:
    def test_relative(self):
        ref = parse_reference("Patient/p1")
        assert (ref.resource_type, ref.resource_id, ref.absolute) == ("Patient", "p1", False)

    def test_absolute_with_history(self):
        ref = parse_reference("https://fhir.example.org/r4/Patient/p1/_history/2")
        assert (ref.resource_type, ref.resource_id, ref.absolute) == ("Patient", "p1", True)

    def test_urn_uuid(self):
        ref = parse_reference("urn:uuid:1234-abcd")
        assert (ref.resource_type, ref.resource_id) == (None, "1234-abcd")

    @pytest.mark.parametrize("value", ["p1", "patient/p1", "Patient/", "Patient/bad id", "urn:uuid:"])
    def test_malformed(self, value):
        assert parse_reference(value) is None


def test_r4_resource_types():
    assert {"Patient", "Condition", "Observation", "Bundle", "Encounter"} <= R4_RESOURCE_TYPES
    assert "Patinet" not in R4_RESOURCE_TYPES
    assert len(R4_RESOURCE_TYPES) == 146
