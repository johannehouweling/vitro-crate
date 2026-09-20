"""The recorded application version names the commit it was built from (#770).

Every crate before this claimed ``0.1.0`` — 687 commits under one version
string — so a crate could not be tied back to the code that wrote it.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from builder.state import GeneratorInfo, _app_version, _git_local_version

# PEP 440 local version identifier: "+" then dot-separated alphanumerics.
PEP440_LOCAL = re.compile(r"^\+g[0-9a-f]{7,40}(\.dirty)?$")


def _repo_with_one_commit(root: Path) -> Path:
    """Make ``root`` a one-commit git repo; return the committed file."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    tracked = root / "a.txt"
    tracked.write_text("hello\n")
    for args in (
        ["init", "-q"],
        ["add", "a.txt"],
        ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
    return tracked


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

    def test_uncommitted_change_is_marked_dirty(self, tmp_path: Path) -> None:
        tracked = _repo_with_one_commit(tmp_path)
        tracked.write_text("changed\n")
        assert _git_local_version(tmp_path).endswith(".dirty")

    def test_clean_checkout_is_never_marked_dirty(self, tmp_path: Path) -> None:
        tracked = _repo_with_one_commit(tmp_path)
        # A touched-but-identical file and an untracked scratch file are both
        # clean: neither changes what the commit says the code is.
        os.utime(tracked, (time.time() + 5, time.time() + 5))
        (tmp_path / "output.jsonld").write_text("{}")
        segment = _git_local_version(tmp_path)
        assert PEP440_LOCAL.match(segment), segment
        assert not segment.endswith(".dirty")

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

    def test_both_entry_points_carry_the_commit(self) -> None:
        captured = GeneratorInfo.capture().version
        assert captured == GeneratorInfo().version
        # Not just equal — equal at the NEW value. `capture()` used to read
        # ``__version__`` straight, which matched the old default's bare
        # "0.1.0" and so agreed without ever naming the commit.
        _, plus, local = captured.partition("+")
        assert plus and PEP440_LOCAL.match(plus + local), captured
