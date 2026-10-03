"""Security-related behaviour: hostile file names, spreadsheet formulas, and what reaches the logs."""

from __future__ import annotations

import io
import logging

from pathlib import Path

import pandas as pd

from healthdata_insight.reports import csv_bytes
from healthdata_insight.ui.common import md_escape


APP = Path(__file__).resolve().parents[1] / "app.py"


def test_markdown_escape_neutralizes_images_and_links():
    escaped = md_escape("![x](https://example.org/p.png) [click](javascript:alert(1)) a_b*c.json")
    assert "![" not in escaped and "](" not in escaped
    assert escaped.replace("\\", "") == "![x](https://example.org/p.png) [click](javascript:alert(1)) a_b*c.json"


def test_csv_neutralizes_formulas_but_not_numbers():
    frame = pd.DataFrame({"text": ["=HYPERLINK(\"http://x\")", "+1", "-cmd", "@SUM(A1)", "\tx", "plain", None],
                          "number": [-1.5, 2, 3, 4, 5, 6, 7]})
    rows = pd.read_csv(io.BytesIO(csv_bytes(frame)), keep_default_na=False)
    assert list(rows["text"]) == ["'=HYPERLINK(\"http://x\")", "'+1", "'-cmd", "'@SUM(A1)", "'\tx", "plain", ""]
    assert rows["number"].iloc[0] == -1.5


def test_unexpected_ingest_errors_log_the_hash_not_the_file_name(monkeypatch, caplog, tmp_path):
    from streamlit.testing.v1 import AppTest

    import healthdata_insight.ingest as ingest_module
    from healthdata_insight.persistence import DB_PATH_ENV

    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "history.db"))

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ingest_module, "parse_fhir_file", explode)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    caplog.set_level(logging.ERROR)
    at.selectbox(key="sample_set").set_value("Clean synthetic data").run()
    assert not at.exception
    assert any("unexpected error" in e.value for e in at.error)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "sha256=" in logged and "synthetic_bundle" not in logged
    at.session_state.store.cleanup()


def test_processing_failure_shows_an_error_not_a_traceback(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    import healthdata_insight.pipeline as pipeline_module
    from healthdata_insight.persistence import DB_PATH_ENV

    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "history.db"))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.selectbox(key="sample_set").set_value("Clean synthetic data").run()

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline_module, "analyze_store", explode)
    [process] = [b for b in at.button if b.label == "Process dataset"]
    process.click().run()
    assert not at.exception
    assert any("Processing failed" in e.value for e in at.error)
    assert not at.tabs  # no half-built dashboard
    at.session_state.store.cleanup()
