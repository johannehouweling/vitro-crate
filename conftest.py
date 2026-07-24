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
