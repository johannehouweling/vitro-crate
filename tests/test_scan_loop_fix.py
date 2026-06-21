"""Tests for the scan re-loop fix.

Two compounding causes made the agent re-scan forever:
  #51 — xlsx/xls were text-sampled, so binary zip bytes (PK\\x03\\x04…) landed
        in first_rows as mojibake.
  #61 — the scan tool handed the LLM the raw list[FileClassification] (1468
        objects), a huge blob with no clear success signal.

These tests cover the binary-sampling skip and the compact LLM-facing summary.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from builder.state import FileClassification
from builder.tools.scanner import (
    read_file_sample,
    scan_files,
    summarize_scan_result,
)


def _make_xlsx(path: Path) -> None:
    """Write a minimal real .xlsx (a zip; bytes begin with PK\\x03\\x04)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")


class TestBinarySamplingSkip:
    """#51 — binary office formats must not be text-sampled into first_rows."""

    def test_read_file_sample_returns_none_for_xlsx(self, tmp_path):
        f = tmp_path / "data.xlsx"
        _make_xlsx(f)
        assert read_file_sample(str(f)) is None

    def test_read_file_sample_returns_none_for_nul_bytes(self, tmp_path):
        f = tmp_path / "weird.csv"
        f.write_bytes(b"col1,col2\n\x00\x01\x02 binary junk")
        assert read_file_sample(str(f)) is None

    def test_read_file_sample_still_reads_text_csv(self, tmp_path):
        f = tmp_path / "ok.csv"
        f.write_text("a,b\n1,2\n")
        out = read_file_sample(str(f))
        assert out is not None
        assert "a,b" in out

    def test_scan_files_leaves_xlsx_first_rows_none(self, tmp_path):
        _make_xlsx(tmp_path / "book.xlsx")
        results = scan_files(str(tmp_path))
        xlsx = next(r for r in results if r.filename == "book.xlsx")
        assert xlsx.first_rows is None


class TestSummarizeScanResult:
    """#61 — the LLM-facing summary is compact and signals success."""

    def _files(self, n: int) -> list[FileClassification]:
        return [
            FileClassification(
                path=f"/d/f{i}.csv",
                filename=f"f{i}.csv",
                size=10,
                mime_type="text/csv",
                first_rows=["a,b", "1,2"],
            )
            for i in range(n)
        ]

    def test_compact_with_success_signal_and_count(self):
        s = summarize_scan_result(self._files(1468))
        assert isinstance(s, str)
        assert "1468" in s
        assert "state" in s.lower()       # tells the LLM results are stored
        assert len(s) < 2000              # not the giant raw blob
        assert "1,2" not in s             # no first_rows content leaked

    def test_bounded_sample_of_filenames(self):
        s = summarize_scan_result(self._files(1468), sample=15)
        assert "f0.csv" in s
        assert "more" in s.lower()        # indicates truncation

    def test_empty_scan(self):
        s = summarize_scan_result([])
        assert "0" in s


class TestScanToolWrapperReturnsSummary:
    """#61 — the LangChain wrapper returns the summary, not the raw list."""

    def test_wrapper_summarizes_scan_files(self, monkeypatch):
        from builder.agents.agent_loop import _build_langchain_tools
        from builder.engine import AgentEngine

        engine = AgentEngine()
        fake_files = [
            FileClassification(
                path=f"/d/f{i}.csv",
                filename=f"f{i}.csv",
                size=1,
                mime_type="text/csv",
            )
            for i in range(1468)
        ]
        monkeypatch.setattr(
            engine,
            "run_tool",
            lambda name, **kw: fake_files if name == "scan_files" else None,
        )

        tools = _build_langchain_tools(engine)
        scan_tool = next(t for t in tools if t.name == "scan_files")
        result = scan_tool.invoke({"path": "/d"})

        assert isinstance(result, str)
        assert "1468" in result
        assert len(result) < 2000

    def test_wrapper_passes_through_non_scan_tools(self, monkeypatch):
        from builder.agents.agent_loop import _build_langchain_tools
        from builder.engine import AgentEngine

        engine = AgentEngine()
        monkeypatch.setattr(
            engine, "run_tool", lambda name, **kw: {"ok": True, "tool": name}
        )
        tools = _build_langchain_tools(engine)
        validate_tool = next(t for t in tools if t.name == "validate")
        result = validate_tool.invoke({"crate_path": "/x"})
        assert result == {"ok": True, "tool": "validate"}
