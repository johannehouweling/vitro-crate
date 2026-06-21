"""Tests for the structured profiling log (ProfilingLogger).

These tests verify the ProfilingLogger writes correct NDJSON,
handles edge cases gracefully, and integrates with AgentEngine.
"""

from __future__ import annotations

import json
import logging


class TestProfilingLogger:
    """Tests for the ProfilingLogger class."""

    def _make_logger(self, tmp_path, session_id="test-session"):
        """Create a ProfilingLogger with SESSION_DIR overridden to tmp_path."""
        import builder.tools.profiler as profiler_mod
        from builder.tools.profiler import ProfilingLogger
        self._orig_dir = profiler_mod.SESSION_DIR
        profiler_mod.SESSION_DIR = tmp_path / "sessions"
        return ProfilingLogger(session_id)

    def _restore_dir(self):
        """Restore original SESSION_DIR."""
        import builder.tools.profiler as profiler_mod
        if hasattr(self, '_orig_dir'):
            profiler_mod.SESSION_DIR = self._orig_dir

    def test_creates_profile_file(self, tmp_path):
        """ProfilingLogger creates the profile.ndjson file."""
        pl = self._make_logger(tmp_path, "test-001")
        try:
            profile_path = tmp_path / "sessions" / "test-001" / "profile.ndjson"
            assert profile_path.exists(), f"Expected {profile_path} to exist"
            assert pl._file is not None
            assert not pl._silent
        finally:
            pl.close()
            self._restore_dir()

    def test_log_event_writes_valid_ndjson(self, tmp_path):
        """log_event writes a valid JSON line to the file."""
        pl = self._make_logger(tmp_path, "test-002")
        try:
            pl.log_event("test_event", tool="my_tool", duration_ms=123.4, iteration=5)

            profile_path = tmp_path / "sessions" / "test-002" / "profile.ndjson"
            lines = profile_path.read_text().strip().splitlines()
            assert len(lines) == 1

            record = json.loads(lines[0])
            assert record["event"] == "test_event"
            assert record["tool"] == "my_tool"
            assert record["duration_ms"] == 123.4
            assert record["iteration"] == 5
            assert "timestamp" in record
        finally:
            pl.close()
            self._restore_dir()

    def test_log_tool_call_convenience(self, tmp_path):
        """log_tool_call writes a tool_call event with correct fields."""
        pl = self._make_logger(tmp_path, "test-003")
        try:
            pl.log_tool_call("scan_files", duration_ms=5000.0, iteration=1,
                             args="{'path': '/data'}")

            profile_path = tmp_path / "sessions" / "test-003" / "profile.ndjson"
            lines = profile_path.read_text().strip().splitlines()
            assert len(lines) == 1

            record = json.loads(lines[0])
            assert record["event"] == "tool_call"
            assert record["tool"] == "scan_files"
            assert record["duration_ms"] == 5000.0
            assert record["iteration"] == 1
            assert "'path'" in record["args"]
        finally:
            pl.close()
            self._restore_dir()

    def test_multiple_events_appended(self, tmp_path):
        """Multiple log_event calls append lines to the same file."""
        pl = self._make_logger(tmp_path, "test-004")
        try:
            pl.log_event("event_1")
            pl.log_event("event_2", tool="foo")
            pl.log_event("event_3", duration_ms=999.9)

            profile_path = tmp_path / "sessions" / "test-004" / "profile.ndjson"
            lines = profile_path.read_text().strip().splitlines()
            assert len(lines) == 3

            records = [json.loads(line) for line in lines]
            assert records[0]["event"] == "event_1"
            assert records[1]["event"] == "event_2"
            assert records[2]["event"] == "event_3"
            assert records[2]["duration_ms"] == 999.9
        finally:
            pl.close()
            self._restore_dir()

    def test_empty_session_id_silent(self):
        """ProfilingLogger with empty session_id operates in silent mode."""
        from builder.tools.profiler import ProfilingLogger

        pl = ProfilingLogger("")
        try:
            assert pl._silent
            assert pl._file is None
            pl.log_event("should_not_write")
            pl.log_tool_call("should_not_write", duration_ms=1.0, iteration=0)
        finally:
            pl.close()

    def test_close_is_idempotent(self, tmp_path):
        """Calling close() multiple times does not crash."""
        pl = self._make_logger(tmp_path, "test-005")
        pl.close()
        pl.close()
        pl.log_event("after_close")
        assert pl._file is None
        assert pl._silent
        self._restore_dir()

    def test_log_event_extra_fields(self, tmp_path):
        """Additional kwargs are included in the event record."""
        pl = self._make_logger(tmp_path, "test-006")
        try:
            pl.log_event("custom", extra_field="hello", node_count=42)

            profile_path = tmp_path / "sessions" / "test-006" / "profile.ndjson"
            record = json.loads(profile_path.read_text().strip())
            assert record["event"] == "custom"
            assert record["extra_field"] == "hello"
            assert record["node_count"] == 42
        finally:
            pl.close()
            self._restore_dir()

    def test_unwritable_dir_fallback(self, tmp_path, caplog):
        """ProfilingLogger degrades gracefully when sessions dir is unwritable."""
        import os
        caplog.set_level(logging.WARNING)
        import builder.tools.profiler as profiler_mod
        from builder.tools.profiler import ProfilingLogger

        # Put a regular file where the session directory should be so
        # mkdir fails when ProfilingLogger tries to create the path.
        session_root = tmp_path / "sessions_root"
        session_root.mkdir(parents=True, exist_ok=True)
        session_root.rmdir()
        session_root.write_text("i am a file")

        self._orig_dir = profiler_mod.SESSION_DIR
        profiler_mod.SESSION_DIR = session_root
        try:
            pl = ProfilingLogger("fail-session")
            assert pl._silent
            assert pl._file is None

            warnings = [r for r in caplog.records
                        if "could not open" in r.getMessage().lower()]
            assert len(warnings) >= 1

            pl.log_event("should_not_crash")
            pl.log_tool_call("should_not_crash", duration_ms=1.0, iteration=0)
            pl.close()
        finally:
            profiler_mod.SESSION_DIR = self._orig_dir
class TestProfilerEngineIntegration:
    """Tests for ProfilingLogger integration with AgentEngine."""

    def test_engine_initializes_profiler(self):
        """AgentEngine.initialize() creates a ProfilingLogger."""
        from builder.engine import AgentEngine

        engine = AgentEngine()
        assert engine.profiler is None

        engine.initialize()
        assert engine.profiler is not None
        assert not engine.profiler._silent
        engine.close_profiler()

    def test_run_tool_writes_profile_entry(self, tmp_path):
        """run_tool writes a tool_call event to the profiler."""
        import builder.tools.profiler as profiler_mod
        from builder.engine import AgentEngine
        from builder.tools.profiler import ProfilingLogger

        orig = profiler_mod.SESSION_DIR
        profiler_mod.SESSION_DIR = tmp_path / "sessions"
        try:
            engine = AgentEngine()
            engine.initialize()
            engine.profiler = ProfilingLogger(engine.state.session_id)

            engine.run_tool("draft_investigation", hints={"name": "Test"})

            profile_path = tmp_path / "sessions" / engine.state.session_id / "profile.ndjson"
            assert profile_path.exists()
            lines = profile_path.read_text().strip().splitlines()
            assert len(lines) >= 1

            record = json.loads(lines[0])
            assert record["event"] == "tool_call"
            assert record["tool"] == "draft_investigation"
            assert record["duration_ms"] > 0
            assert record["iteration"] == 1
        finally:
            profiler_mod.SESSION_DIR = orig
            engine.close_profiler()

    def test_multiple_tool_calls_logged(self, tmp_path):
        """Multiple run_tool calls produce multiple profile entries."""
        import builder.tools.profiler as profiler_mod
        from builder.engine import AgentEngine
        from builder.tools.profiler import ProfilingLogger

        orig = profiler_mod.SESSION_DIR
        profiler_mod.SESSION_DIR = tmp_path / "sessions"
        try:
            engine = AgentEngine()
            engine.initialize()
            engine.profiler = ProfilingLogger(engine.state.session_id)

            engine.run_tool("draft_investigation", hints={"name": "One"})
            engine.run_tool("draft_investigation", hints={"name": "Two"})
            engine.run_tool("draft_investigation", hints={"name": "Three"})

            profile_path = tmp_path / "sessions" / engine.state.session_id / "profile.ndjson"
            lines = profile_path.read_text().strip().splitlines()
            assert len(lines) == 3

            iterations = [json.loads(l)["iteration"] for l in lines]
            assert iterations == [1, 2, 3]
        finally:
            profiler_mod.SESSION_DIR = orig
            engine.close_profiler()

    def test_close_profiler_stops_writing(self, tmp_path):
        """After close_profiler, no more events are written."""
        import builder.tools.profiler as profiler_mod
        from builder.engine import AgentEngine
        from builder.tools.profiler import ProfilingLogger

        orig = profiler_mod.SESSION_DIR
        profiler_mod.SESSION_DIR = tmp_path / "sessions"
        try:
            engine = AgentEngine()
            engine.initialize()
            engine.profiler = ProfilingLogger(engine.state.session_id)

            engine.run_tool("draft_investigation", hints={"name": "Before"})
            engine.close_profiler()
            assert engine.profiler is None

            engine.run_tool("draft_investigation", hints={"name": "After"})

            profile_path = tmp_path / "sessions" / engine.state.session_id / "profile.ndjson"
            lines = profile_path.read_text().strip().splitlines()
            assert len(lines) == 1
        finally:
            profiler_mod.SESSION_DIR = orig