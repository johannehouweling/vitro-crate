"""Pure-logic tests for the dashboard ▶/⏸ agent status indicator (#193).

These tests live in ``tests/test_dashboard_status.py`` *deliberately*: CI runs
``pytest --ignore=tests/test_dashboard.py`` (see ``.github/workflows/ci.yml``),
so anything placed in ``test_dashboard.py`` is silently skipped by the gate.
Keeping the status-inference tests in this separate, *not-ignored* file means
they actually run in CI.

They cover the pure helper :func:`builder.tools.dashboard.determine_agent_status`,
which classifies a list of profiler records into one of:

    "driving"  -- ▶ agent actively working (a tool/node in progress)
    "waiting"  -- ⏸ agent blocked on a human (pending HITL call)
    "idle"     -- ⏹ no activity / terminal

No Rich rendering is exercised here — the logic is intentionally pure so it is
fast and offline.
"""

from __future__ import annotations

from builder.tools.dashboard import (
    STATUS_DRIVING,
    STATUS_IDLE,
    STATUS_WAITING,
    determine_agent_status,
)

# ---------------------------------------------------------------------------
# idle
# ---------------------------------------------------------------------------


def test_no_records_is_idle() -> None:
    """An empty profile means nothing has happened yet → idle."""
    assert determine_agent_status([]) == STATUS_IDLE


def test_terminal_node_end_is_idle() -> None:
    """A completed node with no pending work afterwards → idle."""
    records = [
        {"event": "node_start", "node": "model", "timestamp": "2026-06-21T12:00:00"},
        {"event": "node_end", "node": "model", "timestamp": "2026-06-21T12:00:01"},
    ]
    assert determine_agent_status(records) == STATUS_IDLE


def test_completed_tool_then_node_end_is_idle() -> None:
    """A finished tool round-trip that ends on a node_end → idle."""
    records = [
        {"event": "node_start", "node": "tools", "timestamp": "2026-06-21T12:00:00"},
        {"event": "tool_call", "tool": "scan_files", "duration_ms": 12.0,
         "timestamp": "2026-06-21T12:00:01"},
        {"event": "node_end", "node": "tools", "timestamp": "2026-06-21T12:00:02"},
    ]
    assert determine_agent_status(records) == STATUS_IDLE


# ---------------------------------------------------------------------------
# driving
# ---------------------------------------------------------------------------


def test_node_start_without_end_is_driving() -> None:
    """A node that started but has not ended → agent is working → driving."""
    records = [
        {"event": "node_start", "node": "model", "timestamp": "2026-06-21T12:00:00"},
    ]
    assert determine_agent_status(records) == STATUS_DRIVING


def test_latest_event_is_completed_tool_call_is_driving() -> None:
    """A just-completed tool call (no node_end yet) means the loop is mid-flight."""
    records = [
        {"event": "node_start", "node": "tools", "timestamp": "2026-06-21T12:00:00"},
        {"event": "tool_call", "tool": "lookup_compound", "duration_ms": 8.0,
         "timestamp": "2026-06-21T12:00:01"},
    ]
    assert determine_agent_status(records) == STATUS_DRIVING


def test_unbalanced_starts_is_driving() -> None:
    """More node_starts than node_ends → at least one node still running."""
    records = [
        {"event": "node_start", "node": "model", "timestamp": "2026-06-21T12:00:00"},
        {"event": "node_end", "node": "model", "timestamp": "2026-06-21T12:00:01"},
        {"event": "node_start", "node": "tools", "timestamp": "2026-06-21T12:00:02"},
    ]
    assert determine_agent_status(records) == STATUS_DRIVING


# ---------------------------------------------------------------------------
# waiting (explicit hitl_wait signal — see engine.run_tool)
# ---------------------------------------------------------------------------


def test_pending_present_to_human_is_waiting() -> None:
    """A hitl_wait for present_to_human with no following tool_call → blocked."""
    records = [
        {"event": "node_start", "node": "tools", "timestamp": "2026-06-21T12:00:00"},
        {"event": "hitl_wait", "tool": "present_to_human",
         "timestamp": "2026-06-21T12:00:01"},
    ]
    assert determine_agent_status(records) == STATUS_WAITING


def test_pending_request_input_is_waiting() -> None:
    """A hitl_wait for request_input with no completion → blocked on the human."""
    records = [
        {"event": "hitl_wait", "tool": "request_input",
         "timestamp": "2026-06-21T12:00:01"},
    ]
    assert determine_agent_status(records) == STATUS_WAITING


def test_resolved_hitl_is_not_waiting() -> None:
    """Once the matching tool_call lands, the human responded → no longer waiting."""
    records = [
        {"event": "hitl_wait", "tool": "present_to_human",
         "timestamp": "2026-06-21T12:00:01"},
        {"event": "tool_call", "tool": "present_to_human", "duration_ms": 5000.0,
         "timestamp": "2026-06-21T12:00:06"},
    ]
    # The HITL resolved; the most recent event is a completed tool_call, so the
    # loop is back to driving (model will run next), not waiting.
    assert determine_agent_status(records) == STATUS_DRIVING


def test_waiting_takes_priority_over_open_node() -> None:
    """An open node plus a pending HITL still reports waiting (the human gates all)."""
    records = [
        {"event": "node_start", "node": "tools", "timestamp": "2026-06-21T12:00:00"},
        {"event": "hitl_wait", "tool": "request_input",
         "timestamp": "2026-06-21T12:00:01"},
    ]
    assert determine_agent_status(records) == STATUS_WAITING


def test_later_hitl_wait_after_resolved_one_is_waiting() -> None:
    """A second HITL pause after an earlier resolved one → waiting again."""
    records = [
        {"event": "hitl_wait", "tool": "present_to_human",
         "timestamp": "2026-06-21T12:00:01"},
        {"event": "tool_call", "tool": "present_to_human", "duration_ms": 100.0,
         "timestamp": "2026-06-21T12:00:02"},
        {"event": "node_start", "node": "model", "timestamp": "2026-06-21T12:00:03"},
        {"event": "node_end", "node": "model", "timestamp": "2026-06-21T12:00:04"},
        {"event": "hitl_wait", "tool": "request_input",
         "timestamp": "2026-06-21T12:00:05"},
    ]
    assert determine_agent_status(records) == STATUS_WAITING
