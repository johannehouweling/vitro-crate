"""Binary files with textual MIME/extension must not loop the agent (#101).

A real dataset zip (GraphPad .prism/.xls/.xlsx) surfaced two bugs:
- read_file_sample (content mode) returns a bare None for binary files, so the
  weak model re-calls it forever and hits the iteration cap.
- read_file_sample (summary mode) routed .xls/.xlsx/.prism (textual MIME) to the
  CSV summarizer and read the raw zip/OLE2 header as "Columns: PK\\x03\\x04...".
"""

from __future__ import annotations

import pytest

from builder.tools.scanner import read_file_sample


def _write_binary(path, name):
    """Write a file that looks textual by extension but is binary (zip magic + NULs)."""
    p = path / name
    p.write_bytes(b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 256)
    return p


class TestSummaryBinaryGuard:
    def test_binary_csv_extension_not_summarized_as_csv(self, tmp_path):
        """A binary file mislabeled text/csv must not be parsed into garbage columns."""
        p = _write_binary(tmp_path, "data.csv")
        result = read_file_sample(str(p), mode="summary")
        # Either None (refused) — never a CSV summary built from binary bytes.
        if result is not None:
            assert "PK\x03\x04" not in result
            assert "Columns" not in result

    def test_binary_prism_extension_not_garbage(self, tmp_path):
        p = _write_binary(tmp_path, "plate.prism")
        result = read_file_sample(str(p), mode="summary")
        if result is not None:
            assert "PK\x03\x04" not in result

    def test_real_xlsx_summarized_as_excel_not_csv(self, tmp_path):
        """A real .xlsx must reach the Excel summarizer, not the CSV one."""
        openpyxl = pytest.importorskip("openpyxl")
        f = tmp_path / "book.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["a", "b"])
        ws.append([1, 2])
        wb.save(f)
        result = read_file_sample(str(f), mode="summary")
        assert result is not None
        assert "CSV/TSV" not in result
        assert "Excel" in result or "xlsx" in result.lower()


class TestAgentUnreadableMessage:
    def test_message_helper_is_actionable(self):
        from builder.agents.react.agent_loop import _unreadable_file_message

        msg = _unreadable_file_message("/data/raw/sample.xls")
        assert isinstance(msg, str) and msg
        assert "sample.xls" in msg
        low = msg.lower()
        assert "skip" in low or "do not retry" in low or "don't retry" in low

    def test_agent_tool_returns_message_not_none_for_binary(self, tmp_path):
        """The LLM-facing read_file_sample tool returns guidance, never bare None."""
        pytest.importorskip("langchain_core")
        from builder.agents.react.agent_loop import _build_langchain_tools
        from builder.engine import AgentEngine

        p = _write_binary(tmp_path, "blob.prism")
        tools = {t.name: t for t in _build_langchain_tools(AgentEngine())}
        out = tools["read_file_sample"].invoke({"path": str(p)})
        assert isinstance(out, str)
        assert out  # non-empty actionable message instead of None

    def test_message_names_the_offending_tool(self):
        # Issue #148: the recovery message is now reused across the file readers,
        # so it must name whichever tool produced the bare None.
        from builder.agents.react.agent_loop import _unreadable_file_message

        msg = _unreadable_file_message("/data/sheet.xlsx", "read_excel")
        assert "read_excel" in msg
        assert "sheet.xlsx" in msg

    @pytest.mark.parametrize("tool_name", ["read_file", "read_excel", "read_docx"])
    def test_other_readers_return_message_not_none(self, tmp_path, tool_name):
        # Issue #148: read_file / read_excel / read_docx previously handed the
        # LLM a bare None for missing/binary/too-large files; they must now get
        # the same actionable "skip it" guidance as read_file_sample.
        pytest.importorskip("langchain_core")
        from builder.agents.react.agent_loop import _build_langchain_tools
        from builder.engine import AgentEngine

        p = _write_binary(tmp_path, "blob.xlsx")
        tools = {t.name: t for t in _build_langchain_tools(AgentEngine())}
        out = tools[tool_name].invoke({"path": str(p)})
        assert isinstance(out, str)
        assert out
        assert tool_name in out
