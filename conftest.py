"""Shared pytest fixtures for the ISA-Tox RO-Crate Builder test suite."""

from __future__ import annotations

import pytest


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