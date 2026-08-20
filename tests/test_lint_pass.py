"""Tests that the source code passes ruff linting.

This guards the CI lint step, so it runs **the command CI runs** — bare
``ruff check``, whose scope pyproject already sets (tests/ excluded). It used to
pass ``builder/``, which is narrower: a violation in ``main.py``, ``eval/`` or
``scripts/`` passed here and failed CI, while the docstring claimed otherwise.
"""

from __future__ import annotations

import subprocess
import sys


def test_ruff_lint_passes() -> None:
    """Ruff lint must pass with zero errors everywhere CI checks."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"ruff check failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )