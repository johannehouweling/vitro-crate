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

        if hasattr(self, "_orig_dir"):
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
            pl.log_tool_call(
                "scan_files", duration_ms=5000.0, iteration=1, args="{'path': '/data'}"
            )

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

            warnings = [r for r in caplog.records if "could not open" in r.getMessage().lower()]
            assert len(warnings) >= 1

            pl.log_event("should_not_crash")
            pl.log_tool_call("should_not_crash", duration_ms=1.0, iteration=0)
            pl.close()
        finally:
            profiler_mod.SESSION_DIR = self._orig_dir

    def test_all_profile_lines_have_required_fields(self, tmp_path):
        """Every line in profile.ndjson has 'event' and 'timestamp' fields."""
        pl = self._make_logger(tmp_path, "test-required-fields")
        try:
            pl.log_event("event_a", tool="tool_1", duration_ms=10.0, iteration=1)
            pl.log_event("event_b", node="model")
            pl.log_event("event_c", duration_ms=99.9, iteration=2)
            pl.log_event("event_d", extra_field="xyz")
            pl.log_tool_call("my_tool", duration_ms=500.0, iteration=3)
            pl.log_event("event_e")

            profile_path = tmp_path / "sessions" / "test-required-fields" / "profile.ndjson"
            lines = profile_path.read_text().strip().splitlines()
            assert len(lines) == 6, f"Expected 6 lines, got {len(lines)}"

            for i, line in enumerate(lines):
                record = json.loads(line)
                assert "event" in record, f"Line {i} missing 'event': {line}"
                assert "timestamp" in record, f"Line {i} missing 'timestamp': {line}"
                assert isinstance(record["event"], str), (
                    f"Line {i} event is not str: {type(record['event'])}"
                )
                assert isinstance(record["timestamp"], str), (
                    f"Line {i} timestamp is not str: {type(record['timestamp'])}"
                )
        finally:
            pl.close()
            self._restore_dir()


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
        """run_tool writes a tool_start marker and then a completed tool_call."""
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
            records = [json.loads(line) for line in profile_path.read_text().strip().splitlines()]

            # A live "tool_start" marker is emitted before entering the tool so
            # profile.ndjson does not look idle during a slow call; the completed
            # record with the duration is still written after it returns.
            assert [r["event"] for r in records] == ["tool_start", "tool_call"]
            assert records[0]["tool"] == "draft_investigation"

            record = records[1]
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
            records = [json.loads(line) for line in profile_path.read_text().strip().splitlines()]

            # Each call contributes a "tool_start" marker plus its completed
            # "tool_call"; the iteration counter is stamped on the completed one.
            completed = [r for r in records if r["event"] == "tool_call"]
            assert len(completed) == 3
            assert [r["iteration"] for r in completed] == [1, 2, 3]
            assert len([r for r in records if r["event"] == "tool_start"]) == 3
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
            records = [json.loads(line) for line in profile_path.read_text().strip().splitlines()]
            # Only the pre-close call is recorded (its "tool_start" + "tool_call");
            # the post-close call writes nothing at all.
            assert [r["event"] for r in records] == ["tool_start", "tool_call"]
            assert all(r["tool"] == "draft_investigation" for r in records)
        finally:
            profiler_mod.SESSION_DIR = orig
