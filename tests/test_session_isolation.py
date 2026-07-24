"""Session working-dir isolation.

Session artifacts are rooted at a single, configurable location
(``builder.config.session_root()``), overridable with the ``VITRO_SESSION_DIR``
environment variable. The three consumers that historically hard-coded
``Path("sessions")`` (``profiler``, ``session``, ``dashboard``) derive their
``SESSION_DIR`` from that resolver, and an autouse conftest fixture redirects it
to a throwaway tmp dir so the test suite never litters the repo's real
``sessions/`` folder (the cause of ~1,800 stray session dirs).
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestSessionRootResolver:
    def test_defaults_to_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no override, the session root is the relative ``sessions/`` dir."""
        monkeypatch.delenv("VITRO_SESSION_DIR", raising=False)
        from builder.config import session_root

        assert session_root() == Path("sessions")

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """``VITRO_SESSION_DIR`` relocates the session root."""
        target = tmp_path / "custom_sessions"
        monkeypatch.setenv("VITRO_SESSION_DIR", str(target))
        from builder.config import session_root

        assert session_root() == target


class TestSessionDirIsolation:
    """The autouse fixture in conftest.py must redirect SESSION_DIR off the repo."""

    def test_session_dir_redirected_away_from_repo(self) -> None:
        from builder.tools import dashboard as dashboard_mod
        from builder.tools import profiler as profiler_mod
        from builder.tools import session as session_mod

        for mod in (profiler_mod, session_mod, dashboard_mod):
            assert mod.SESSION_DIR != Path("sessions"), (
                f"{mod.__name__}.SESSION_DIR still points at the repo sessions/ dir; "
                "the autouse isolation fixture is not active"
            )

    def test_profiler_write_stays_in_isolated_dir(self) -> None:
        from builder.tools import profiler as profiler_mod

        sid = "isolation_probe_sid"
        logger = profiler_mod.ProfilingLogger(sid)
        try:
            logger.log_event("tool_call", tool="scan_files", duration_ms=1.0, iteration=1)
        finally:
            logger.close()

        # Written under the redirected (tmp) root...
        assert (profiler_mod.SESSION_DIR / sid / "profile.ndjson").exists()
        # ...and NOT under the repo's real sessions/ dir (the regression we guard).
        assert not (Path("sessions") / sid).exists()
