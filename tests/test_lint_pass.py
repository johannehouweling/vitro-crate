"""Tests that the source code passes ruff linting.

This prevents regression on the CI lint step (uvx ruff check).
Note: ruff's pyproject.toml excludes tests/ from checking, so this
test only verifies the builder/ source code, not the tests themselves.
"""

from __future__ import annotations

import subprocess
import sys


def test_ruff_lint_passes() -> None:
    """Ruff lint must pass with zero errors on builder/ source code."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "builder/"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"ruff check failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )