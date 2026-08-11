"""Shared pytest fixtures for the ISA-Tox RO-Crate Builder test suite."""

from __future__ import annotations

import importlib
import os
import tempfile

import pytest

# Isolate session artifacts for the WHOLE test session before any test module
# imports builder.tools.{profiler,session,dashboard}: their module-level
# ``SESSION_DIR`` (= builder.config.session_root()) is evaluated at import time,
# so the override must be in place first. This backstops the per-test fixture
# below for any session write that resolves SESSION_DIR outside a test body
# (collection-time construction, background threads, teardown races).
os.environ.setdefault("VITRO_SESSION_DIR", tempfile.mkdtemp(prefix="vitro-test-sessions-"))


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect all session working data to a throwaway tmp dir for every test.

    Production roots session artifacts at ``builder.config.session_root()``
    (``sessions/`` by default). Without this, every test that builds an
    engine/profiler would write a real ``sessions/<id>/`` dir into the repo
    (historically ~1,800 stray dirs, many empty). Sets ``VITRO_SESSION_DIR``
    and repoints each consumer's already-imported module-level ``SESSION_DIR``
    so both the resolver and the cached constants stay isolated. Tests that
    override ``SESSION_DIR`` themselves still win — they run after this setup.
    """
    root = tmp_path / "sessions"
    monkeypatch.setenv("VITRO_SESSION_DIR", str(root))
    from builder import config

    for name in (
        "builder.tools.profiler",
        "builder.tools.session",
        "builder.tools.dashboard",
    ):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "SESSION_DIR", config.session_root(), raising=False)
    return root


@pytest.fixture(autouse=True)
def _reset_http_shared_state():
    """Clear ``lookups._http``'s process-global per-host state around every test.

    ``_http`` deliberately keeps two module-level caches that outlive a single
    lookup: the circuit breaker (``_breaker_state``) and the politeness throttle
    (``_host_throttle``). Production wants exactly that — one host's outage is
    remembered for the rest of the run. A test process is a *very long* run, so
    without this fixture one test's simulated failures leak into every later
    test in the same worker that touches the same host.

    Concretely: three tests that drive a timeout/429/5xx at ``api.test`` push
    the breaker over ``_BREAKER_THRESHOLD``, and for the next 60 seconds
    ``http_get_json`` raises ``TransientLookupError`` for that host *without
    issuing a request at all*. A later test then either fails outright (nothing
    to inspect in ``responses.calls``) or — worse — passes vacuously, because
    the "transient failure" it thinks it provoked came from the breaker rather
    than from the response it registered. Which of those happens depends on how
    xdist packs tests into workers, so it surfaces as load-dependent flakiness
    instead of a stable red (#406).

    Resetting *after* the test too keeps a test's own failures from reaching
    fixture teardown or the next module's collection-time code.
    """
    from lookups import _http

    _http.reset_circuit_breaker()
    _http.reset_host_throttle()
    yield
    _http.reset_circuit_breaker()
    _http.reset_host_throttle()


@pytest.fixture
def tmp_files(tmp_path):
    """Fixture that provides a helper to create files in a temporary directory.

    Usage in tests::

        def test_something(tmp_files):
            tmp_files.create("hello.txt", b"hello world")
            # tmp_files.path is the temporary directory Path
    """

    class _TmpFiles:
        def __init__(self, path):
            self.path = path
            self.path.mkdir(parents=True, exist_ok=True)

        def create(self, name: str, content: bytes | str = b"") -> None:
            """Create a file with the given name and content."""
            if isinstance(content, str):
                content = content.encode("utf-8")
            filepath = self.path / name
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(content)

    return _TmpFiles(tmp_path)
