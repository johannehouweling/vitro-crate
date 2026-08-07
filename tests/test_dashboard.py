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


class _DummyLive:
    """A stand-in for ``rich.live.Live`` that records every ``update()`` call."""

    def __init__(self, *a, **k) -> None:
        self.updates: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False

    def update(self, renderable, *a, **k) -> None:
        self.updates.append(renderable)


class TestLiveRefresh:
    """Regression guards for the live dashboard auto-refresh (#267).

    Two confirmed root causes, both fixed here:

    1. The old ``_change_touches`` basename filter discarded the atomic-save
       signal. ``save_session`` writes ``crate_state.json`` via tempfile +
       ``os.replace``; on macOS the watch batch contains ONLY the temp file
       (``.crate_state_tmp_*``), which the filter explicitly EXCLUDED — so
       ``live.update()`` never fired. The filter is gone; we render on EVERY
       wake (event OR timeout).
    2. The session was pinned at startup (``sessions[0]`` once). A fresh
       ``--interactive`` run creates a NEW session dir the dashboard never
       followed. Now, with no explicit ``session_id``, the loop re-resolves the
       newest session on every wake.

    Earlier #121 fix retained: ``_read_records_cached`` (mtime cache) so
    ``profile.ndjson`` isn't needlessly re-parsed.
    """

    def test_watch_is_called_with_session_dir_root_and_event_or_timeout(
        self, tmp_path, monkeypatch
    ) -> None:
        """``watch()`` must poll the SESSION_DIR root with ``yield_on_timeout``
        and a ``rust_timeout`` derived from ``refresh_interval`` — so the loop
        wakes on every change AND at least every ``refresh_interval`` even with
        zero events (the bulletproof poll fallback vs FSEvents quirks)."""
        import rich.live
        import watchfiles

        from builder.tools import dashboard as d

        session_dir = tmp_path / "20260626_a"
        session_dir.mkdir()
        (session_dir / "profile.ndjson").write_text(
            json.dumps({"event": "node_end", "node": "model"}) + "\n"
        )
        monkeypatch.setattr(d, "SESSION_DIR", tmp_path)

        captured: dict = {}

        def fake_watch(*paths, **kwargs):
            captured["paths"] = paths
            captured["rust_timeout"] = kwargs.get("rust_timeout")
            captured["yield_on_timeout"] = kwargs.get("yield_on_timeout")
            return iter(())  # no wakes -> loop body skipped, returns immediately

        monkeypatch.setattr(watchfiles, "watch", fake_watch)
        monkeypatch.setattr(rich.live, "Live", _DummyLive)

        d._run_live_dashboard(session_id=None, refresh_interval=2.0)

        # Watch the SESSION_DIR ROOT, not an individual session/file.
        assert captured["paths"] == (str(tmp_path),)
        assert captured["yield_on_timeout"] is True
        assert captured["rust_timeout"] == int(2.0 * 1000)

    def test_renders_on_timeout_wake_with_no_events(self, tmp_path, monkeypatch) -> None:
        """A timeout/empty wake (the poll path) must trigger a render — NOT only
        a filtered file event. This is the core #267 fix: even when the atomic
        save surfaces solely as temp-file churn (or no event at all), the loop
        still rebuilds and renders."""
        import rich.live
        import watchfiles

        from builder.tools import dashboard as d

        session_dir = tmp_path / "20260626_a"
        session_dir.mkdir()
        (session_dir / "profile.ndjson").write_text(
            json.dumps({"event": "node_end", "node": "model"}) + "\n"
        )
        monkeypatch.setattr(d, "SESSION_DIR", tmp_path)

        live = _DummyLive()

        def fake_watch(*paths, **kwargs):
            # One empty (timeout) batch, then stop.
            yield set()

        monkeypatch.setattr(watchfiles, "watch", fake_watch)
        monkeypatch.setattr(rich.live, "Live", lambda *a, **k: live)

        d._run_live_dashboard(session_id=None, refresh_interval=2.0)

        # The empty/timeout wake produced a render.
        assert len(live.updates) >= 1

    def test_renders_on_temp_file_only_event(self, tmp_path, monkeypatch) -> None:
        """When the only change surfaced is a ``.crate_state_tmp_*`` event (the
        macOS atomic-save signature), the loop must STILL render. The old
        basename filter dropped this exact event — that was the bug."""
        import rich.live
        import watchfiles

        from builder.tools import dashboard as d

        session_dir = tmp_path / "20260626_a"
        session_dir.mkdir()
        (session_dir / "profile.ndjson").write_text(
            json.dumps({"event": "node_end", "node": "model"}) + "\n"
        )
        monkeypatch.setattr(d, "SESSION_DIR", tmp_path)

        live = _DummyLive()
        tmp_evt = str(session_dir / ".crate_state_tmp_abc")

        def fake_watch(*paths, **kwargs):
            yield {(1, tmp_evt)}  # deleted/added temp churn only

        monkeypatch.setattr(watchfiles, "watch", fake_watch)
        monkeypatch.setattr(rich.live, "Live", lambda *a, **k: live)

        d._run_live_dashboard(session_id=None, refresh_interval=2.0)

        assert len(live.updates) >= 1

    def test_follows_newest_session_per_wake_when_unpinned(
        self, tmp_path, monkeypatch
    ) -> None:
        """With no explicit ``session_id``, the loop re-resolves the newest
        session on EVERY wake via ``list_sessions_available`` and renders it —
        so a fresh ``--interactive`` run is followed live, no restart (#267)."""
        import rich.live
        import watchfiles

        from builder.tools import dashboard as d

        old = tmp_path / "20260626_old"
        old.mkdir()
        (old / "profile.ndjson").write_text(
            json.dumps({"event": "node_end", "node": "model"}) + "\n"
        )
        monkeypatch.setattr(d, "SESSION_DIR", tmp_path)

        live = _DummyLive()
        consults: list = []
        formatted: list = []

        real_list = d.list_sessions_available

        def spy_list(*a, **k):
            result = real_list(*a, **k)
            consults.append([s["session_id"] for s in result])
            return result

        def fake_format(session_id, records):
            formatted.append(session_id)
            return f"render::{session_id}"

        def fake_watch(*paths, **kwargs):
            # First wake: only the old session exists.
            yield set()
            # Between wakes a NEW (newer) session dir appears.
            new = tmp_path / "20260627_new"
            new.mkdir()
            (new / "profile.ndjson").write_text(
                json.dumps({"event": "node_end", "node": "model"}) + "\n"
            )
            # Make the new dir unambiguously newer by mtime.
            import os
            import time

            t = time.time() + 100
            os.utime(new, (t, t))
            yield set()

        monkeypatch.setattr(d, "list_sessions_available", spy_list)
        monkeypatch.setattr(d, "format_session_summary", fake_format)
        monkeypatch.setattr(watchfiles, "watch", fake_watch)
        monkeypatch.setattr(rich.live, "Live", lambda *a, **k: live)

        d._run_live_dashboard(session_id=None, refresh_interval=2.0)

        # list_sessions_available consulted on every wake (not just startup).
        assert len(consults) >= 2
        # The newest session was rendered after it appeared.
        assert "20260627_new" in formatted
        assert formatted[-1] == "20260627_new"

    def test_explicit_session_id_stays_pinned(self, tmp_path, monkeypatch) -> None:
        """An explicitly passed ``session_id`` stays pinned across wakes even if
        a newer session dir appears — the dashboard must not wander."""
        import rich.live
        import watchfiles

        from builder.tools import dashboard as d

        pinned = tmp_path / "20260626_pinned"
        pinned.mkdir()
        (pinned / "profile.ndjson").write_text(
            json.dumps({"event": "node_end", "node": "model"}) + "\n"
        )
        monkeypatch.setattr(d, "SESSION_DIR", tmp_path)

        live = _DummyLive()
        formatted: list = []

        def fake_format(session_id, records):
            formatted.append(session_id)
            return f"render::{session_id}"

        def fake_watch(*paths, **kwargs):
            # A newer session appears, but we must stay pinned.
            newer = tmp_path / "20260627_newer"
            newer.mkdir()
            (newer / "profile.ndjson").write_text(
                json.dumps({"event": "node_end", "node": "model"}) + "\n"
            )
            import os
            import time

            t = time.time() + 100
            os.utime(newer, (t, t))
            yield set()

        monkeypatch.setattr(d, "format_session_summary", fake_format)
        monkeypatch.setattr(watchfiles, "watch", fake_watch)
        monkeypatch.setattr(rich.live, "Live", lambda *a, **k: live)

        d._run_live_dashboard(session_id="20260626_pinned", refresh_interval=2.0)

        assert formatted, "should have rendered"
        assert set(formatted) == {"20260626_pinned"}

    def test_no_crash_when_session_dir_has_no_sessions(
        self, tmp_path, monkeypatch
    ) -> None:
        """SESSION_DIR exists but is empty (no sessions yet) — the loop must wake,
        render an empty state, and not crash while waiting for one to appear."""
        import rich.live
        import watchfiles

        from builder.tools import dashboard as d

        monkeypatch.setattr(d, "SESSION_DIR", tmp_path)  # empty dir, no sessions

        live = _DummyLive()

        def fake_watch(*paths, **kwargs):
            yield set()  # one timeout wake, then stop

        monkeypatch.setattr(watchfiles, "watch", fake_watch)
        monkeypatch.setattr(rich.live, "Live", lambda *a, **k: live)

        # Must not raise.
        d._run_live_dashboard(session_id=None, refresh_interval=2.0)

    def test_static_fallback_when_watchfiles_unavailable(
        self, tmp_path, monkeypatch
    ) -> None:
        """When ``watchfiles`` can't be imported, fall back to a static snapshot
        (printed once) instead of crashing."""
        import builtins

        from builder.tools import dashboard as d

        session_dir = tmp_path / "20260626_a"
        session_dir.mkdir()
        (session_dir / "profile.ndjson").write_text(
            json.dumps({"event": "node_end", "node": "model"}) + "\n"
        )
        monkeypatch.setattr(d, "SESSION_DIR", tmp_path)

        real_import = builtins.__import__

        def blocked_import(name, *a, **k):
            if name == "watchfiles":
                raise ImportError("watchfiles not installed")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", blocked_import)

        # Must not raise — falls back to a one-shot static render.
        d._run_live_dashboard(session_id=None, refresh_interval=2.0)

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


class TestFormatMitCoverage:
    """format_mit_coverage() — the shared MIT-tile formatter (issue #355).

    One source of truth for the dashboard panel and the interactive UI so both
    build arms render MIT coverage identically. ``overall_score`` is a 0.0-1.0
    fraction; the formatter returns ``(text, rich_color)``.
    """

    def test_fraction_rendered_as_whole_percent(self) -> None:
        from builder.tools.dashboard import format_mit_coverage

        assert format_mit_coverage(0.85, assessed=True) == ("85%", "green")

    def test_full_coverage_is_100_not_1(self) -> None:
        from builder.tools.dashboard import format_mit_coverage

        assert format_mit_coverage(1.0, assessed=True) == ("100%", "green")

    def test_middling_score_is_yellow(self) -> None:
        from builder.tools.dashboard import format_mit_coverage

        assert format_mit_coverage(0.6, assessed=True) == ("60%", "yellow")

    def test_low_score_is_red(self) -> None:
        from builder.tools.dashboard import format_mit_coverage

        assert format_mit_coverage(0.3, assessed=True) == ("30%", "red")

    def test_unassessed_is_neutral_placeholder_not_zero_percent(self) -> None:
        """A never-assessed report must not read as a red 0% — it is unknown."""
        from builder.tools.dashboard import format_mit_coverage

        text, color = format_mit_coverage(0.0, assessed=False)
        assert text == "—"
        assert color == "dim"
        assert "%" not in text

    def test_none_score_is_neutral(self) -> None:
        from builder.tools.dashboard import format_mit_coverage

        assert format_mit_coverage(None, assessed=False) == ("—", "dim")


class TestCrateStatePanelMit:
    """_build_cratestate_panel() renders MIT via the shared formatter (issue #355)."""

    @staticmethod
    def _render(state: dict) -> str:
        from rich.console import Console

        from builder.tools.dashboard import _build_cratestate_panel

        console = Console(width=200)
        with console.capture() as capture:
            console.print(_build_cratestate_panel(state))
        return capture.get()

    def test_covered_crate_shows_whole_percent(self) -> None:
        state = {
            "entities": {},
            "validation": {},
            "mit_assessment": {
                "module_scores": {"m1": {"completed": 17, "total": 20}},
                "overall_score": 0.85,
            },
        }
        out = self._render(state)
        assert "85%" in out
        assert "0.85%" not in out

    def test_full_coverage_not_one_percent(self) -> None:
        state = {
            "entities": {},
            "validation": {},
            "mit_assessment": {
                "module_scores": {"m1": {"completed": 20, "total": 20}},
                "overall_score": 1.0,
            },
        }
        out = self._render(state)
        assert "100%" in out
        assert "1.0%" not in out

    def test_unassessed_not_shown_as_zero_percent(self) -> None:
        state = {
            "entities": {},
            "validation": {},
            "mit_assessment": {"module_scores": {}, "overall_score": 0.0},
        }
        out = self._render(state)
        assert "MIT" in out
        assert "0.0%" not in out
        assert "0%" not in out



class TestLastExportRow:
    """The CrateState panel reports where the crate was written, and when.

    NOTE: CI passes ``--ignore=tests/test_dashboard.py``, so these do not gate.
    The state-level behaviour they depend on is covered by
    ``tests/test_state_serializer.py::TestExportStamp``, which does run.
    """

    @staticmethod
    def _render(metadata: dict) -> str:
        import io

        from rich.console import Console

        from builder.tools.dashboard import _build_cratestate_panel

        buf = io.StringIO()
        Console(file=buf, width=120, color_system=None).print(
            _build_cratestate_panel({"metadata": metadata, "entities": {}, "validation": {}})
        )
        return buf.getvalue()

    def test_never_exported_says_so(self) -> None:
        out = self._render({})
        assert "Last export: never" in out

    def test_shows_path_and_formatted_time(self) -> None:
        out = self._render(
            {
                "output_path": "/data/S-VHPS21-ro-crate",
                "exported_at": "2026-08-07T11:42:31.123456+02:00",
            }
        )
        assert "/data/S-VHPS21-ro-crate" in out
        assert "2026-08-07 11:42" in out
        assert "never" not in out

    def test_path_without_a_stamp_is_not_reported_as_never(self) -> None:
        """A session saved before exports were stamped DID export.

        Calling that "never" would misreport work that happened — the whole
        reason this row exists is to tell someone their crate is on disk.
        """
        out = self._render({"output_path": "/data/old-crate"})
        assert "/data/old-crate" in out
        assert "time not recorded" in out
        assert "Last export: never" not in out

    def test_unparseable_stamp_falls_back_to_the_raw_value(self) -> None:
        out = self._render({"output_path": "/data/x", "exported_at": "not-a-date"})
        assert "not-a-date" in out
