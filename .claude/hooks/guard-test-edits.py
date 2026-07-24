#!/usr/bin/env python3
"""PreToolUse guard: make ``tests/`` read-only during implementation.

On agentic-coding benchmarks (ImpossibleBench) the dominant reward-hack for
Claude-family models is editing or weakening the very tests that gate the task, to
force a green bar. This hook denies ``Edit``/``Write``/``MultiEdit`` on any path
under ``tests/`` so that weakening a test is a *deliberate, visible* act rather than
a silent default: set ``CLAUDE_ALLOW_TEST_EDITS=1`` to author or update tests
(the TDD red-first step).

Contract (Claude Code PreToolUse hook): reads the tool call as JSON on stdin
(``tool_name`` + ``tool_input.file_path``, absolute); exit 0 = allow, exit 2 =
deny (stderr is shown to the model). Any parsing failure allows, so the guard can
never wedge a session.
"""

from __future__ import annotations

import json
import os
import sys

_GUARDED_TOOLS = {"Edit", "Write", "MultiEdit"}


def decide(payload: dict, env: dict[str, str]) -> str | None:
    """Return a denial reason for a guarded test-file edit, else ``None`` (allow)."""
    if env.get("CLAUDE_ALLOW_TEST_EDITS") == "1":
        return None
    if payload.get("tool_name") not in _GUARDED_TOOLS:
        return None
    file_path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not file_path:
        return None

    project = env.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ""
    try:
        rel = (
            os.path.relpath(os.path.abspath(file_path), os.path.abspath(project))
            if project
            else file_path
        )
    except ValueError:  # e.g. different drive on Windows
        rel = file_path
    first = rel.replace("\\", "/").split("/", 1)[0]
    if first != "tests":
        return None

    return (
        f"Blocked: {payload.get('tool_name')} on {rel} — tests/ is read-only during "
        "implementation. Weakening a test to make code pass is the top agentic "
        "reward-hack, so it must be a deliberate act: if you are legitimately "
        "authoring or updating tests (TDD red-first), re-run with "
        "CLAUDE_ALLOW_TEST_EDITS=1 set."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    reason = decide(payload, dict(os.environ))
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
