"""The recorded application version names the commit it was built from (#770).

Every crate before this claimed ``0.1.0`` — 687 commits under one version
string — so a crate could not be tied back to the code that wrote it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from builder.state import GeneratorInfo, _app_version, _git_local_version

# PEP 440 local version identifier: "+" then dot-separated alphanumerics.
PEP440_LOCAL = re.compile(r"^\+g[0-9a-f]{7,40}(\.dirty)?$")


class TestGitLocalVersion:
    """The local-version segment, best-effort and silent."""

    def test_checkout_names_the_short_head_sha(self) -> None:
        segment = _git_local_version()
        assert PEP440_LOCAL.match(segment), segment
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert segment.startswith(f"+g{head}")

    def test_app_version_appends_the_segment(self) -> None:
        version = _app_version()
        assert version.endswith(_git_local_version())
        assert version.split("+")[0], version

    def test_not_a_checkout_falls_back_to_the_bare_version(self, tmp_path: Path) -> None:
        assert _git_local_version(tmp_path) == ""

    def test_git_unavailable_falls_back_to_the_bare_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".git").mkdir()

        def _no_git(*args: object, **kwargs: object) -> object:
            raise OSError("git: command not found")

        monkeypatch.setattr(subprocess, "run", _no_git)
        assert _git_local_version(tmp_path) == ""


class TestCaptureAgreesWithTheDefault:
    """Both call sites route through ``_app_version``, so a BASE check cannot flap."""

    def test_capture_version_matches_the_default(self) -> None:
        assert GeneratorInfo.capture().version == GeneratorInfo().version
