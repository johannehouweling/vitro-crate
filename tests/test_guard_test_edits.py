"""Contract tests for the read-only-tests PreToolUse guard (.claude/hooks).

Exercises the hook the way Claude Code invokes it — a JSON tool-call on stdin, a
process exit code out (0 = allow, 2 = deny) — so the test pins the real contract,
not an importable helper. See ``.claude/hooks/guard-test-edits.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".claude" / "hooks" / "guard-test-edits.py"


def _run(payload: dict, *, allow: bool = False) -> subprocess.CompletedProcess[str]:
    env = {"CLAUDE_PROJECT_DIR": str(REPO_ROOT), "PATH": "/usr/bin:/bin"}
    if allow:
        env["CLAUDE_ALLOW_TEST_EDITS"] = "1"
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _edit(rel_path: str, tool: str = "Edit") -> dict:
    return {
        "tool_name": tool,
        "cwd": str(REPO_ROOT),
        "tool_input": {"file_path": str(REPO_ROOT / rel_path)},
    }


class TestBlocksTestEdits:
    def test_editing_a_test_file_is_denied(self) -> None:
        result = _run(_edit("tests/test_foo.py"))
        assert result.returncode == 2
        assert "read-only" in result.stderr.lower()
        assert "CLAUDE_ALLOW_TEST_EDITS" in result.stderr

    def test_writing_a_fixture_is_denied(self) -> None:
        result = _run(_edit("tests/fixtures/some_input.json", tool="Write"))
        assert result.returncode == 2

    def test_multiedit_on_tests_is_denied(self) -> None:
        assert _run(_edit("tests/test_foo.py", tool="MultiEdit")).returncode == 2


class TestAllows:
    def test_override_env_var_allows_test_edits(self) -> None:
        assert _run(_edit("tests/test_foo.py"), allow=True).returncode == 0

    def test_non_test_file_is_allowed(self) -> None:
        assert _run(_edit("builder/tools/validation.py")).returncode == 0

    def test_a_path_merely_containing_tests_is_allowed(self) -> None:
        # 'contests/' or 'builder/tests_helpers.py' must NOT be caught — only the
        # top-level tests/ directory is guarded.
        assert _run(_edit("builder/contests/thing.py")).returncode == 0

    def test_non_guarded_tool_is_allowed(self) -> None:
        assert _run(_edit("tests/test_foo.py", tool="Read")).returncode == 0


class TestFailsOpen:
    def test_malformed_stdin_allows(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input="not json",
            capture_output=True,
            text=True,
            env={"CLAUDE_PROJECT_DIR": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0

    def test_missing_file_path_allows(self) -> None:
        payload = {"tool_name": "Edit", "cwd": str(REPO_ROOT), "tool_input": {}}
        assert _run(payload).returncode == 0
