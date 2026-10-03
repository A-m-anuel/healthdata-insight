from __future__ import annotations

from collections import Counter

import pytest

from conftest import resource, to_bytes, to_ndjson
from healthdata_insight.config import MAX_FILE_SIZE_BYTES
from healthdata_insight.parser import FHIRFileError, FileFormat, parse_fhir_file


def types(parsed) -> Counter:
    return Counter(r["resourceType"] for r in parsed.resources)


class TestJson:
    def test_bundle_is_unpacked(self, bundle):
        parsed = parse_fhir_file("data.json", to_bytes(bundle))
        assert parsed.format is FileFormat.JSON
        assert parsed.bundle_count == 1
        assert types(parsed) == {"Patient": 2, "Condition": 1, "Observation": 2}
        assert parsed.issues == []

    def test_single_resource(self):
        parsed = parse_fhir_file("p.json", to_bytes(resource("Patient", "p1")))
        assert types(parsed) == {"Patient": 1}
        assert parsed.bundle_count == 0

    def test_array_of_resources(self):
        data = to_bytes([resource("Patient", "p1"), resource("Observation", "o1")])
        assert types(parse_fhir_file("a.json", data)) == {"Patient": 1, "Observation": 1}

    def test_nested_bundle(self, bundle):
        outer = {"resourceType": "Bundle", "entry": [{"resource": bundle}]}
        parsed = parse_fhir_file("nested.json", to_bytes(outer))
        assert parsed.bundle_count == 2
        assert len(parsed.resources) == 5

    def test_metadata(self, bundle):
        data = to_bytes(bundle)
        parsed = parse_fhir_file("data.json", data)
        assert parsed.name == "data.json"
        assert parsed.size_bytes == len(data)
        assert len(parsed.sha256) == 64

    def test_utf8_bom_is_accepted(self):
        data = b"\xef\xbb\xbf" + to_bytes(resource("Patient", "p1"))
        assert types(parse_fhir_file("bom.json", data)) == {"Patient": 1}

    def test_ndjson_with_json_extension_falls_back(self):
        data = to_ndjson([resource("Patient", "p1"), resource("Patient", "p2")])
        parsed = parse_fhir_file("export.json", data)
        assert parsed.format is FileFormat.NDJSON
        assert len(parsed.resources) == 2
        assert "contains NDJSON" in parsed.issues[0].message

    def test_bundle_problems_become_issues(self):
        data = to_bytes({
            "resourceType": "Bundle",
            "entry": [{"resource": resource("Patient", "p1")}, {"fullUrl": "x"}, "junk",
                      {"resource": {"id": "no-type"}}],
        })
        parsed = parse_fhir_file("b.json", data)
        assert types(parsed) == {"Patient": 1}
        assert len(parsed.issues) == 3


class TestNdjson:
    def test_lines_are_parsed(self):
        data = to_ndjson([resource("Patient", "p1"), resource("Condition", "c1"), resource("Observation", "o1")])
        parsed = parse_fhir_file("data.ndjson", data)
        assert parsed.format is FileFormat.NDJSON
        assert types(parsed) == {"Patient": 1, "Condition": 1, "Observation": 1}

    def test_jsonl_extension(self):
        assert parse_fhir_file("data.jsonl", to_ndjson([resource("Patient", "p1")])).format is FileFormat.NDJSON

    def test_blank_lines_and_crlf_are_ignored(self):
        data = b'{"resourceType":"Patient"}\r\n\r\n   \r\n{"resourceType":"Patient"}\r\n'
        parsed = parse_fhir_file("data.ndjson", data)
        assert len(parsed.resources) == 2
        assert parsed.issues == []

    def test_bad_lines_are_skipped_with_line_numbers(self):
        data = b'{"resourceType":"Patient"}\n{not json}\n{"id":"x"}\n{"resourceType":"Observation"}\n'
        parsed = parse_fhir_file("data.ndjson", data)
        assert types(parsed) == {"Patient": 1, "Observation": 1}
        assert [w.line for w in parsed.issues] == [2, 3]
        assert str(parsed.issues[0]).startswith("Line 2:")

    def test_bundle_on_a_line(self, bundle):
        parsed = parse_fhir_file("data.ndjson", to_ndjson([bundle, resource("Patient", "p3")]))
        assert types(parsed)["Patient"] == 3


class TestErrors:
    @pytest.mark.parametrize("data", [b"", b"   \n\t  "])
    def test_empty_file(self, data):
        with pytest.raises(FHIRFileError, match="empty"):
            parse_fhir_file("empty.json", data)

    def test_invalid_json_reports_position(self):
        with pytest.raises(FHIRFileError, match=r"Invalid JSON at line 1, column \d+"):
            parse_fhir_file("bad.json", b'{"resourceType": "Patient",')

    def test_not_utf8(self):
        with pytest.raises(FHIRFileError, match="UTF-8"):
            parse_fhir_file("latin1.json", '{"resourceType":"Patient","name":"José"}'.encode("latin-1"))

    def test_unsupported_extension(self):
        with pytest.raises(FHIRFileError, match="Unsupported file type"):
            parse_fhir_file("data.csv", b"a,b\n1,2")

    def test_too_large(self, monkeypatch):
        monkeypatch.setattr("healthdata_insight.parser.MAX_FILE_SIZE_BYTES", 10)
        with pytest.raises(FHIRFileError, match="size limit"):
            parse_fhir_file("big.json", to_bytes(resource("Patient", "p1")))
        assert MAX_FILE_SIZE_BYTES > 10

    @pytest.mark.parametrize("value", [{"hello": "world"}, [1, 2, 3], "text", {"resourceType": ""}])
    def test_json_without_fhir_resources(self, value):
        with pytest.raises(FHIRFileError, match="No FHIR resources") as excinfo:
            parse_fhir_file("other.json", to_bytes(value))
        assert excinfo.value.issues

    def test_empty_bundle(self):
        with pytest.raises(FHIRFileError, match="No FHIR resources") as excinfo:
            parse_fhir_file("b.json", to_bytes({"resourceType": "Bundle", "entry": []}))
        assert "no entries" in excinfo.value.issues[0].message

    def test_ndjson_with_only_bad_lines(self):
        with pytest.raises(FHIRFileError, match="No FHIR resources"):
            parse_fhir_file("bad.ndjson", b"nope\nstill nope\n")


class TestHostileInput:
    def test_deeply_nested_json_is_rejected_cleanly(self):
        with pytest.raises(FHIRFileError, match="nested too deeply"):
            parse_fhir_file("deep.json", b"[" * 100_000 + b"]" * 100_000)

    def test_deeply_nested_bundles_are_rejected_cleanly(self):
        data = (b'{"resourceType":"Bundle","entry":[{"resource":' * 2_000 + b'{"resourceType":"Patient"}'
                + b"}]}" * 2_000)
        with pytest.raises(FHIRFileError, match="nested too deeply"):
            parse_fhir_file("deep.json", data)

    def test_one_deep_ndjson_line_is_skipped_and_the_rest_read(self):
        data = b'{"resourceType":"Patient","id":"p1"}\n' + b"[" * 100_000 + b"]" * 100_000 + b'\n{"resourceType":"Patient"}\n'
        parsed = parse_fhir_file("mixed.ndjson", data)
        assert len(parsed.resources) == 2
        [issue] = parsed.issues
        assert issue.line == 2 and "nested too deeply" in issue.message
