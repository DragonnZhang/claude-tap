"""End-to-end checks for the export module (markdown / json)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from claude_tap.export import export


def test_export_markdown_to_stdout(sample_jsonl: Path):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = export(sample_jsonl, output=None, fmt="markdown")
    assert rc == 0
    out = buf.getvalue()
    assert "# claude-tap trace" in out
    assert "Turn 1" in out
    assert "claude-opus-4-6" in out


def test_export_json_to_file(tmp_path: Path, sample_jsonl: Path):
    out = tmp_path / "out.json"
    rc = export(sample_jsonl, output=out, fmt=None)  # inferred from extension
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert data[0]["model"] == "claude-opus-4-6"
    assert "messages" in data[0]
    assert "response" in data[0]


def test_export_missing_file_returns_error(tmp_path: Path):
    out = export(tmp_path / "does-not-exist.jsonl", output=None, fmt=None)
    assert out == 1


def test_export_html_writes_self_contained(tmp_path: Path, sample_jsonl: Path):
    out = tmp_path / "trace.html"
    rc = export(sample_jsonl, output=out, fmt="html")
    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "EMBEDDED_TRACE_DATA" in html
