"""Tests for the profiler dashboard."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from builder.tools.dashboard import (
    _build_token_summary,
    _build_token_table,
    format_session_summary,
    list_sessions_available,
    read_profile,
)


class TestReadProfile:
    """read_profile() parses profile.ndjson into structured records."""

    def test_read_profile_parses_ndjson(self) -> None:
        """Parses known events correctly."""
        lines = [
            {
                "event": "tool_call",
                "tool": "scan_files",
                "duration_ms": 1234.5,
                "timestamp": "2026-06-21T12:30:45",
                "iteration": 3,
            },
            {
                "event": "node_start",
                "node": "model",
                "timestamp": "2026-06-21T12:30:46",
            },
            {
                "event": "node_end",
                "node": "model",
                "duration_ms": 6961.3,
                "timestamp": "2026-06-21T12:30:47",
                "iteration": 3,
                "messages_in": 5,
                "messages_out": 1,
                "produced_tool_calls": True,
            },
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
            {
                "event": "tool_call",
                "tool": "scan_files",
                "duration_ms": 100.0,
                "timestamp": "2026-06-21T12:30:45",
                "iteration": 1,
            },
            {
                "event": "tool_call",
                "tool": "scan_files",
                "duration_ms": 200.0,
                "timestamp": "2026-06-21T12:30:46",
                "iteration": 2,
            },
            {
                "event": "tool_call",
                "tool": "draft_investigation",
                "duration_ms": 5.0,
                "timestamp": "2026-06-21T12:30:47",
                "iteration": 3,
            },
            {
                "event": "node_start",
                "node": "model",
                "timestamp": "2026-06-21T12:30:48",
            },
            {
                "event": "node_end",
                "node": "model",
                "duration_ms": 1000.0,
                "timestamp": "2026-06-21T12:30:49",
                "iteration": 3,
            },
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
        assert "Token Usage" in output  # token summary line
        assert "cumulative" in output

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


class TestTokenSummary:
    """_build_token_summary() and _build_token_table()."""

    def test_build_token_summary_empty(self) -> None:
        """No model_end events yields zeros and no last_request."""
        totals, last = _build_token_summary([])
        assert totals["input_tokens"] == 0
        assert totals["output_tokens"] == 0
        assert totals["total_tokens"] == 0
        assert last is None

    def test_build_token_summary_with_tokens(self) -> None:
        """Aggregates input/output tokens across multiple model events."""
        records = [
            {
                "event": "node_end",
                "node": "model",
                "input_tokens": 100,
                "output_tokens": 50,
                "model_name": "gpt-4o",
            },
            {
                "event": "node_end",
                "node": "model",
                "input_tokens": 200,
                "output_tokens": 80,
                "model_name": "gpt-4o",
            },
            {"event": "node_end", "node": "tools"},  # should be ignored
        ]
        totals, last = _build_token_summary(records)
        assert totals["input_tokens"] == 300
        assert totals["output_tokens"] == 130
        assert totals["total_tokens"] == 430
        assert last is not None
        assert last["input_tokens"] == 200
        assert last["output_tokens"] == 80
        assert last["model_name"] == "gpt-4o"

    def test_build_token_summary_partial_tokens(self) -> None:
        """Handles events with only input_tokens or output_tokens missing."""
        records = [
            {"event": "node_end", "node": "model", "input_tokens": 100},
            {"event": "node_end", "node": "model", "output_tokens": 30},
        ]
        totals, last = _build_token_summary(records)
        assert totals["input_tokens"] == 100
        assert totals["output_tokens"] == 30
        assert totals["total_tokens"] == 130

    def test_build_token_table_renders(self) -> None:
        """_build_token_table produces a Rich Table with correct rows."""
        totals: dict[str, int | str | None] = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "model_name": "gpt-4o",
        }
        last_request: dict[str, int | str | None] = {
            "input_tokens": 30,
            "output_tokens": 20,
            "total_tokens": 50,
            "model_name": "gpt-4o",
        }
        table = _build_token_table(totals, last_request)
        from rich.console import Console

        console = Console(width=60)
        with console.capture() as capture:
            console.print(table)
        output = capture.get()
        assert "Token Usage" in output
        assert "Cumulative" in output
        assert "Last request" in output
        assert "100" in output
        assert "50" in output
        assert "gpt-4o" in output

    def test_build_token_table_no_last_request(self) -> None:
        """Token table handles missing last request gracefully."""
        totals: dict[str, int | str | None] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "model_name": None,
        }
        table = _build_token_table(totals, None)
        from rich.console import Console

        console = Console(width=40)
        with console.capture() as capture:
            console.print(table)
        output = capture.get()
        assert "Cumulative" in output
        assert "0" in output


class TestLiveRefresh:
    """Regression guards for the live dashboard auto-refresh (#121).

    The dashboard appeared to "only update on load" because watchfiles' ``step``
    (a debounce quiet-period) was set to the render interval (2000ms), so a
    steady stream of profiler writes never went quiet long enough to be yielded.
    A secondary bug blanked the profile panel whenever only crate_state.json
    changed (profile mtime unchanged -> records became []).
    """

    def test_watch_step_is_small_and_decoupled_from_refresh_interval(
        self, tmp_path, monkeypatch
    ) -> None:
        """``watch()`` must be called with a small fixed ``step`` (the watchfiles
        default), independent of ``refresh_interval`` — otherwise updates are
        throttled to multi-second intervals and the dashboard looks frozen."""
        import rich.live
        import watchfiles

        from builder.tools.dashboard import _WATCH_STEP_MS, _run_live_dashboard

        profile_path = tmp_path / "profile.ndjson"
        profile_path.write_text(json.dumps({"event": "node_end", "node": "model"}) + "\n")

        captured: dict = {}

        def fake_watch(*paths, **kwargs):
            captured["step"] = kwargs.get("step")
            captured["paths"] = paths
            return iter(())  # no changes -> loop body skipped, returns immediately

        class _DummyLive:
            def __init__(self, *a, **k) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a) -> bool:
                return False

            def update(self, *a) -> None:
                pass

        monkeypatch.setattr(watchfiles, "watch", fake_watch)
        monkeypatch.setattr(rich.live, "Live", _DummyLive)

        _run_live_dashboard(profile_path, "sess", refresh_interval=2.0)

        assert _WATCH_STEP_MS <= 100
        assert captured["step"] == _WATCH_STEP_MS
        # The bug was step == int(refresh_interval * 1000) == 2000.
        assert captured["step"] != 2000

    def test_records_cache_reused_when_profile_unchanged(self, tmp_path) -> None:
        """A crate_state-only refresh (profile.ndjson mtime unchanged) reuses the
        cached records instead of returning [] (which blanked the panel)."""
        from builder.tools.dashboard import _read_records_cached

        profile_path = tmp_path / "profile.ndjson"
        profile_path.write_text(json.dumps({"event": "node_end", "node": "model"}) + "\n")

        records, mtime = _read_records_cached(profile_path, 0.0, [])
        assert records, "first read should load records"
        assert mtime != 0.0

        again, mtime2 = _read_records_cached(profile_path, mtime, records)
        assert again == records, "unchanged profile must reuse cached records, not blank"
        assert mtime2 == mtime

    def test_records_cache_rereads_on_change(self, tmp_path) -> None:
        """When profile.ndjson mtime changes, records are re-read fresh."""
        import os

        from builder.tools.dashboard import _read_records_cached

        profile_path = tmp_path / "profile.ndjson"
        profile_path.write_text(json.dumps({"event": "node_end", "node": "model"}) + "\n")

        records, mtime = _read_records_cached(profile_path, 0.0, [])
        assert len(records) == 1

        with open(profile_path, "a") as fh:
            fh.write(json.dumps({"event": "node_end", "node": "tools"}) + "\n")
        os.utime(profile_path, (mtime + 1, mtime + 1))

        records2, mtime2 = _read_records_cached(profile_path, mtime, records)
        assert len(records2) == 2
        assert mtime2 != mtime

    def test_watches_session_directory_so_atomic_saves_are_caught(
        self, tmp_path, monkeypatch
    ) -> None:
        """save_session writes crate_state.json atomically (tempfile + os.replace),
        which swaps the inode a file-path watcher holds — so a watcher bound to the
        FILE silently stops seeing updates and the dashboard only refreshes on
        reload. The fix watches the session DIRECTORY, which catches the rename
        (and crate_state.json first appearing after startup)."""
        import rich.live
        import watchfiles

        from builder.tools.dashboard import _run_live_dashboard

        session_dir = tmp_path / "sess1"
        session_dir.mkdir()
        profile_path = session_dir / "profile.ndjson"
        profile_path.write_text(json.dumps({"event": "node_end", "node": "model"}) + "\n")

        captured: dict = {}

        def fake_watch(*paths, **kwargs):
            captured["paths"] = paths
            return iter(())

        class _DummyLive:
            def __init__(self, *a, **k) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a) -> bool:
                return False

            def update(self, *a) -> None:
                pass

        monkeypatch.setattr(watchfiles, "watch", fake_watch)
        monkeypatch.setattr(rich.live, "Live", _DummyLive)

        _run_live_dashboard(profile_path, "sess1", refresh_interval=2.0)

        # Watch the directory, NOT the individual files (the old, broken behavior
        # watched profile.ndjson / crate_state.json paths and missed atomic renames).
        assert captured["paths"] == (str(session_dir),)

    def test_change_touches_only_relevant_files(self) -> None:
        """The directory watch fires on temp/export churn too; only re-render when
        profile.ndjson or crate_state.json actually changed."""
        from builder.tools.dashboard import _change_touches

        prof = "/s/profile.ndjson"
        crate = "/s/crate_state.json"
        watched = {prof, crate}
        # watchfiles yields a set of (Change, path); the helper inspects only path.
        assert _change_touches({(2, prof)}, watched) is True
        assert _change_touches({(1, crate)}, watched) is True
        # the atomic-save temp churn must NOT trigger a re-render
        assert _change_touches({(1, "/s/.crate_state_tmp_abc")}, watched) is False
        assert _change_touches(set(), watched) is False
