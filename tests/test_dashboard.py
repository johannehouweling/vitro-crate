"""Tests for the profiler dashboard."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from builder.tools.dashboard import (
    format_session_summary,
    list_sessions_available,
    read_profile,
)


class TestReadProfile:
    """read_profile() parses profile.ndjson into structured records."""

    def test_read_profile_parses_ndjson(self) -> None:
        """Parses known events correctly."""
        lines = [
            {"event": "tool_call", "tool": "scan_files", "duration_ms": 1234.5,
             "timestamp": "2026-06-21T12:30:45", "iteration": 3},
            {"event": "node_start", "node": "model",
             "timestamp": "2026-06-21T12:30:46"},
            {"event": "node_end", "node": "model", "duration_ms": 6961.3,
             "timestamp": "2026-06-21T12:30:47", "iteration": 3,
             "messages_in": 5, "messages_out": 1, "produced_tool_calls": True},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            f.flush()
            fname = f.name
        try:
            records = read_profile(Path(fname))
            assert len(records) == 3
            assert records[0]["event"] == "tool_call"
            assert records[0]["tool"] == "scan_files"
            assert records[0]["duration_ms"] == 1234.5
            assert records[1]["event"] == "node_start"
            assert records[1]["node"] == "model"
            assert records[2]["event"] == "node_end"
            assert records[2]["produced_tool_calls"] is True
        finally:
            Path(fname).unlink(missing_ok=True)

    def test_read_profile_empty_file(self) -> None:
        """Empty file returns empty list."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
            fname = f.name
        try:
            records = read_profile(Path(fname))
            assert records == []
        finally:
            Path(fname).unlink(missing_ok=True)

    def test_read_profile_missing_file(self) -> None:
        """Missing file returns empty list."""
        records = read_profile(Path("/tmp/does_not_exist_xyz.ndjson"))
        assert records == []

    def test_read_profile_skips_empty_lines(self) -> None:
        """Blank lines ignored."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
            f.write('{"event": "tool_call", "tool": "test"}\n')
            f.write("\n")
            f.write('{"event": "node_start", "node": "model"}\n')
            f.write("  \n")
            f.flush()
            fname = f.name
        try:
            records = read_profile(Path(fname))
            assert len(records) == 2
        finally:
            Path(fname).unlink(missing_ok=True)


class TestListSessions:
    """list_sessions_available() discovers session dirs."""

    def test_list_sessions_finds_dirs(self) -> None:
        """Finds dirs with profile.ndjson."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "test_session_1"
            session_dir.mkdir()
            (session_dir / "profile.ndjson").write_text("{}")

            empty_dir = Path(tmpdir) / "test_session_2"
            empty_dir.mkdir()

            sessions = list_sessions_available(base_dir=Path(tmpdir))
            ids = [s["session_id"] for s in sessions]
            assert "test_session_1" in ids
            assert "test_session_2" not in ids

    def test_list_sessions_no_sessions_dir(self) -> None:
        """Missing dir returns empty list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions = list_sessions_available(base_dir=Path(tmpdir))
            assert sessions == []


class TestFormatSessionSummary:
    """format_session_summary() produces a Rich renderable."""

    def test_format_session_summary_contains_tables(self) -> None:
        """Returns a Layout with tool and node tables."""
        records = [
            {"event": "tool_call", "tool": "scan_files", "duration_ms": 100.0,
             "timestamp": "2026-06-21T12:30:45", "iteration": 1},
            {"event": "tool_call", "tool": "scan_files", "duration_ms": 200.0,
             "timestamp": "2026-06-21T12:30:46", "iteration": 2},
            {"event": "tool_call", "tool": "draft_investigation", "duration_ms": 5.0,
             "timestamp": "2026-06-21T12:30:47", "iteration": 3},
            {"event": "node_start", "node": "model",
             "timestamp": "2026-06-21T12:30:48"},
            {"event": "node_end", "node": "model", "duration_ms": 1000.0,
             "timestamp": "2026-06-21T12:30:49", "iteration": 3},
        ]
        from rich.layout import Layout
        result = format_session_summary("test-session", records)
        assert isinstance(result, Layout)

        from rich.console import Console
        console = Console(width=100)
        with console.capture() as capture:
            console.print(result)
        output = capture.get()
        assert "test-session" in output
        assert "scan_files" in output
        assert "draft_investigation" in output
        assert "model" in output

    def test_format_session_summary_empty(self) -> None:
        """Shows no-data message for empty records."""
        from rich.layout import Layout
        result = format_session_summary("empty-session", [])
        assert isinstance(result, Layout)

        from rich.console import Console
        console = Console(width=80)
        with console.capture() as capture:
            console.print(result)
        output = capture.get()
        assert "empty-session" in output
        assert "profiling data" in output.lower() or "no data" in output.lower()
