"""Profiler dashboard — Rich TUI for monitoring agent performance.

Provides:
- read_profile() — parse profile.ndjson into structured records
- list_sessions_available() — find session directories with profile data
- format_session_summary() — produce a Rich Layout from records
- run_dashboard() — live-tailing TUI using watchfiles
- run_static_dashboard() — one-shot summary from a session
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSION_DIR = Path("sessions")

# ---------------------------------------------------------------------------
# Data reading
# ---------------------------------------------------------------------------


def read_profile(path: Path) -> list[dict[str, Any]]:
    """Parse a profile.ndjson file into a list of record dicts.

    Returns an empty list if the file does not exist or is empty.
    Blank lines are silently skipped.
    """
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read profile: %s", path)
        return []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            records.append(json.loads(stripped))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed line in %s: %r", path, line[:80])
            continue
    return records


def list_sessions_available(base_dir: Path | None = None) -> list[dict[str, Any]]:
    """List session directories that contain a profile.ndjson file.

    Returns a list of dicts with keys:
        session_id: str
        path: Path
        profile_path: Path
        event_count: int (0 if profile is empty/unreadable)
        last_event: str | None
        file_size: int
    Sorted by modification time, newest first.
    """
    base = base_dir or SESSION_DIR
    if not base.is_dir():
        return []
    sessions: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        profile_path = child / "profile.ndjson"
        if not profile_path.exists():
            continue
        size = profile_path.stat().st_size if profile_path.exists() else 0
        records = read_profile(profile_path) if size > 0 else []
        sessions.append({
            "session_id": child.name,
            "path": child,
            "profile_path": profile_path,
            "event_count": len(records),
            "last_event": records[-1].get("event") if records else None,
            "file_size": size,
        })
    sessions.sort(key=lambda s: s["path"].stat().st_mtime, reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------


def _build_tool_table(records: list[dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    """Aggregate tool_call events into (headers, rows)."""
    tool_calls = [r for r in records if r.get("event") == "tool_call"]
    agg: dict[str, dict[str, float]] = {}
    for tc in tool_calls:
        tool = tc.get("tool", "unknown")
        dur = tc.get("duration_ms", 0.0) or 0.0
        if tool not in agg:
            agg[tool] = {"count": 0, "total": 0.0}
        agg[tool]["count"] += 1
        agg[tool]["total"] += dur
    sorted_tools = sorted(agg.items(), key=lambda x: x[1]["total"], reverse=True)
    headers = ["Tool", "Calls", "Avg (ms)", "Total (s)"]
    rows = []
    for tool, stats in sorted_tools:
        avg = stats["total"] / stats["count"] if stats["count"] else 0
        total_s = stats["total"] / 1000.0
        rows.append([
            tool,
            str(stats["count"]),
            f"{avg:.1f}",
            f"{total_s:.2f}",
        ])
    return headers, rows


def _build_node_table(records: list[dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    """Aggregate node_end events into (headers, rows)."""
    node_ends = [r for r in records if r.get("event") == "node_end"]
    agg: dict[str, dict[str, float]] = {}
    for ne in node_ends:
        node = ne.get("node", "unknown")
        dur = ne.get("duration_ms", 0.0) or 0.0
        if node not in agg:
            agg[node] = {"count": 0, "total": 0.0}
        agg[node]["count"] += 1
        agg[node]["total"] += dur
    sorted_nodes = sorted(agg.items(), key=lambda x: x[1]["total"], reverse=True)
    headers = ["Node", "Calls", "Avg (ms)", "Total (s)"]
    rows = []
    for node, stats in sorted_nodes:
        avg = stats["total"] / stats["count"] if stats["count"] else 0
        total_s = stats["total"] / 1000.0
        rows.append([
            node,
            str(stats["count"]),
            f"{avg:.1f}",
            f"{total_s:.2f}",
        ])
    return headers, rows


def _build_live_events(records: list[dict[str, Any]], max_lines: int = 20) -> list[str]:
    """Build a list of formatted event lines for the live tail."""
    recent = records[-max_lines:] if len(records) > max_lines else records
    lines = []
    for r in recent:
        ts = r.get("timestamp", "")
        evt = r.get("event", "")
        if evt == "tool_call":
            tool = r.get("tool", "?")
            dur = r.get("duration_ms")
            dur_str = f" {dur:.1f}ms" if dur is not None else ""
            lines.append(f"{ts[11:19]}  tool_call   {tool}{dur_str}")
        elif evt == "node_start":
            node = r.get("node", "?")
            lines.append(f"{ts[11:19]}  node_start  {node}")
        elif evt == "node_end":
            node = r.get("node", "?")
            dur = r.get("duration_ms")
            dur_str = f" {dur:.1f}ms" if dur is not None else ""
            lines.append(f"{ts[11:19]}  node_end    {node}{dur_str}")
        else:
            lines.append(f"{ts[11:19]}  {evt}")
    return lines


class _NoDataPanel:
    """Placeholder rendered when there are no records."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def __rich__(self) -> str:
        sid = self._session_id
        return f"[bold yellow]Session:[/] {sid}\n\n[yellow]No profiling data available yet.[/]"


def format_session_summary(
    session_id: str, records: list[dict[str, Any]]
) -> Any:
    """Build a Rich renderable summarising the profiler data.

    Returns a ``rich.layout.Layout`` with tool timing and node timing tables
    plus a live event tail. If *records* is empty, returns a placeholder
    layout saying no data is available.
    """
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.console import Group

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )

    # Header
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_text = Text()
    header_text.append(" Agent Profiler Dashboard", style="bold cyan")
    header_text.append(f"  |  Session: {session_id}", style="cyan")
    layout["header"].update(Panel(header_text, style="cyan"))

    if not records:
        layout["body"].update(_NoDataPanel(session_id))
        layout["footer"].update(Text(f"Last refresh: {now}", style="dim"))
        return layout

    # Build tables
    tool_headers, tool_rows = _build_tool_table(records)
    node_headers, node_rows = _build_node_table(records)
    live_lines = _build_live_events(records)

    tool_table = Table(title="Tool Call Times", header_style="bold magenta")
    for h in tool_headers:
        tool_table.add_column(h)
    for row in tool_rows:
        tool_table.add_row(*row)

    node_table = Table(title="Node Timings", header_style="bold green")
    for h in node_headers:
        node_table.add_column(h)
    for row in node_rows:
        node_table.add_row(*row)

    live_panel = Panel(
        chr(92).join(live_lines[-10:]),
        title="Recent Events",
        border_style="dim",
    )

    # Combine into body
    from rich.columns import Columns
    body = Group(
        Columns([tool_table, node_table], equal=True, expand=True),
        live_panel,
    )
    layout["body"].update(body)
    layout["footer"].update(Text(f"Last refresh: {now}", style="dim"))

    return layout


# ---------------------------------------------------------------------------
# Live dashboard (TUI)
# ---------------------------------------------------------------------------


def _render_static(session_id: str, records: list[dict[str, Any]]) -> None:
    """Render a one-shot summary to stdout."""
    from rich.console import Console
    console = Console()
    layout = format_session_summary(session_id, records)
    console.print(layout)


def run_static_dashboard(session_id: str | None = None) -> None:
    """Run a one-shot dashboard for a given session or the latest one."""
    sessions = list_sessions_available()
    if not sessions:
        print("No session data found. Run the agent first to generate profile data.")
        return

    if session_id is None:
        target = sessions[0]  # newest
    else:
        matches = [s for s in sessions if s["session_id"] == session_id]
        if not matches:
            print(f"Session not found: {session_id}")
            return
        target = matches[0]

    records = read_profile(target["profile_path"])
    _render_static(target["session_id"], records)


def run_dashboard(session_id: str | None = None, refresh_interval: float = 2.0) -> None:
    """Run a live-updating dashboard using watchfiles.

    If *session_id* is None, uses the most recent session with profile data.

    The display refreshes whenever ``profile.ndjson`` is modified — new tool
    calls and node events appear in near-real-time.

    Press Ctrl+C to exit.
    """
    sessions = list_sessions_available()
    if not sessions:
        print("No session data found. Run the agent first to generate profile data.")
        return

    if session_id is None:
        target = sessions[0]
    else:
        matches = [s for s in sessions if s["session_id"] == session_id]
        if not matches:
            print(f"Session not found: {session_id}")
            return
        target = matches[0]

    profile_path = target["profile_path"]

    try:
        _run_live_dashboard(profile_path, target["session_id"], refresh_interval)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nDashboard closed.")


def _run_live_dashboard(
    profile_path: Path,
    session_id: str,
    refresh_interval: float,
) -> None:
    """Inner live-dashboard loop with Rich ``Live`` display and file-watching."""
    from rich.live import Live
    from rich.console import Console

    console = Console()
    last_mtime: float = 0.0

    def _build() -> Layout:
        nonlocal last_mtime
        # Re-read only if the file has changed (avoid pointless I/O)
        try:
            current_mtime = profile_path.stat().st_mtime
        except OSError:
            current_mtime = last_mtime
        records = read_profile(profile_path) if current_mtime != last_mtime else []
        last_mtime = current_mtime
        return format_session_summary(session_id, records)

    try:
        from watchfiles import watch
    except ImportError:
        logger.warning(
            "watchfiles not installed — dashboard will show a static snapshot. "
            "Install with: uv add watchfiles"
        )
        console.print(_build())
        return

    with Live(_build(), console=console, refresh_per_second=1 / refresh_interval, screen=False) as live:
        # watchfiles step is in milliseconds; convert from our seconds-based interval
        step_ms = int(refresh_interval * 1000)
        for _changes in watch(profile_path, step=step_ms):
            live.update(_build())
