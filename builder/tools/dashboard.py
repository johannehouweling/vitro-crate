"""Profiler dashboard — Rich TUI for monitoring agent performance.

Provides:
- read_profile() — parse profile.ndjson into structured records
- list_sessions_available() — find session directories with profile data
- format_session_summary() — produce a Rich Layout from records
  * CrateState overview panel (entity counts, validation, phase, MIT score)
  * Tool timing table (aggregated per tool)
  * Node timing table (aggregated per node)
  * Token usage table (cumulative and last request)
  * Last Agent Response panel (model reply text)
  * Recent Events tail (with tool args & results)
  * Conversation Flow panel (AgentState messages: user prompts, AI replies, tool returns)
- run_dashboard() — live-tailing TUI using watchfiles
- run_static_dashboard() — one-shot summary from a session
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import builder.config as _config

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
        sessions.append(
            {
                "session_id": child.name,
                "path": child,
                "profile_path": profile_path,
                "event_count": len(records),
                "last_event": records[-1].get("event") if records else None,
                "file_size": size,
            }
        )
    sessions.sort(key=lambda s: s["path"].stat().st_mtime, reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# CrateState loading
# ---------------------------------------------------------------------------


def _load_cratestate(session_id: str) -> dict[str, Any] | None:
    """Load the CrateState JSON for the given session.

    Returns the parsed dict, or None if the file doesn't exist or is corrupt.
    """
    state_path = SESSION_DIR / session_id / "crate_state.json"
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not load crate_state.json for %s", session_id)
        return None


# ---------------------------------------------------------------------------
# CrateState overview panel
# ---------------------------------------------------------------------------


def _build_cratestate_panel(state: dict[str, Any] | None) -> Any:
    """Build a Rich Panel summarising CrateState.

    Shows phase, entity counts, validation status, MIT score, iteration count.
    Returns a placeholder Panel when *state* is None.
    """
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    if state is None:
        return Panel(
            "[dim]No CrateState data available.[/dim]",
            title="CrateState Overview",
            border_style="blue",
        )

    # Phase
    phase = _determine_phase_from_state(state)

    # Entity counts
    entities = state.get("entities", {})
    entity_counts: list[tuple[str, int]] = []
    total = 0
    for etype, coll in entities.items():
        if isinstance(coll, list):
            count = len(coll)
            if count > 0:
                entity_counts.append((etype, count))
                total += count

    # Validation status
    val = state.get("validation", {})
    base_ok = val.get("base_passed", False)
    isa_ok = val.get("isa_passed", False)
    tox_ok = val.get("tox_passed", False)
    required_count = len(val.get("required_issues", []))

    # MIT score
    mit = state.get("mit_assessment", {})
    mit_score = mit.get("overall_score", 0)

    # Iteration
    iteration = state.get("iteration_count", 0)
    stuck = state.get("stuck", False)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")

    # Phase with colour
    phase_colors = {
        "initial": "dim",
        "scanning": "yellow",
        "drafting": "cyan",
        "validating": "magenta",
        "complete": "green",
        "stuck": "red",
    }
    pc = phase_colors.get(phase, "white")
    table.add_row("Phase", f"[{pc}]{phase}[/{pc}]")

    # Entity summary
    entity_parts = [f"{n}={c}" for n, c in entity_counts[:6]]
    if len(entity_counts) > 6:
        entity_parts.append(f"...+{total - sum(c for _, c in entity_counts[:6])}")
    table.add_row("Entities", f"[cyan]{total}[/cyan]  ({', '.join(entity_parts)})")

    # Validation
    val_parts = []
    val_parts.append(f"{'✓' if base_ok else '✗'} Base")
    val_parts.append(f"{'✓' if isa_ok else '✗'} ISA")
    val_parts.append(f"{'✓' if tox_ok else '✗'} Tox")
    val_color = "green" if (base_ok and isa_ok and tox_ok) else "red" if required_count > 0 else "yellow"
    table.add_row("Validation", f"[{val_color}]{'  '.join(val_parts)}[/{val_color}]")
    if required_count > 0:
        table.add_row("  Issues", f"[red]{required_count} REQUIRED[/red]")

    # MIT
    mit_color = "green" if mit_score >= 80 else "yellow" if mit_score >= 50 else "red"
    table.add_row("MIT Score", f"[{mit_color}]{mit_score}%[/{mit_color}]")

    # Iteration
    it_color = "red" if stuck else "white"
    table.add_row("Iteration", f"[{it_color}]{iteration}{' [red]STUCK[/red]' if stuck else ''}[/{it_color}]")

    return Panel(table, title="CrateState Overview", border_style="blue")


def _determine_phase_from_state(state: dict[str, Any]) -> str:
    """Determine the current build phase from CrateState dict."""
    if state.get("stuck"):
        return "stuck"
    checkpoint = state.get("checkpoint", {})
    completed = checkpoint.get("completed_checkpoints", [])
    if "crate_built" in completed:
        return "complete"
    if "files_scanned" in completed:
        return "drafting"
    scanned_files = state.get("scanned_files", [])
    if scanned_files:
        return "scanning"
    entities = state.get("entities", {})
    for coll in entities.values():
        if isinstance(coll, list) and len(coll) > 0:
            return "drafting"
    return "initial"


# ---------------------------------------------------------------------------
# Tool categorisation for the aggregate table
# ---------------------------------------------------------------------------
# When adding new tools, add an entry here so they land in the right category.
# Tools not listed here fall into "Other" -- visible in the dashboard but
# flagged with a warning so we know to categorise them.

_TOOL_CATEGORIES: dict[str, str] = {
    # Drafting entities
    "draft_investigation": "Drafting",
    "draft_study": "Drafting",
    "draft_assay": "Drafting",
    "draft_process": "Drafting",
    "draft_protocol": "Drafting",
    "draft_sample": "Drafting",
    "draft_molecular_entity": "Drafting",
    "draft_cell_line_sample": "Drafting",
    "draft_person": "Drafting",
    "draft_organization": "Drafting",
    "draft_publication": "Drafting",
    "draft_defined_term": "Drafting",
    "draft_property_value": "Drafting",
    # Entity management
    "list_entities": "Management",
    "update_entity": "Management",
    "remove_entity": "Management",
    "set_entity_field": "Management",
    "bulk_set_fields": "Management",
    # Lookups
    "lookup_compound": "Lookups",
    "lookup_cell_line": "Lookups",
    "lookup_aop": "Lookups",
    "lookup_bao_term": "Lookups",
    "lookup_orcid": "Lookups",
    "lookup_ror": "Lookups",
    "lookup_doi": "Lookups",
    # File tools
    "scan_files": "Files",
    "read_file_sample": "Files",
    "read_multiple_files": "Files",
    "preview_archive": "Files",
    "extract_pdf_text": "Files",
    "extract_pdf_tables": "Files",
    # Verification
    "verify_identifier": "Verify",
    "verify_all_identifiers": "Verify",
    # Crate assembly & validation
    "build_crate": "Crate",
    "validate": "Crate",
    # Assessment
    "assess_mit_coverage": "Assess",
    "assess_fair_maturity": "Assess",
    # Session & HITL
    "save_session": "Session",
    "load_session": "Session",
    "list_sessions": "Session",
    "get_status": "Session",
    "get_hint": "Session",
    "present_to_human": "HITL",
    "request_input": "HITL",
}

_CATEGORY_ORDER = [
    "Drafting",
    "Management",
    "Lookups",
    "Files",
    "Verify",
    "Crate",
    "Assess",
    "Session",
    "HITL",
    "Other",
]

_UNCATEGORISED = "Other"

# Track which tools hit the fallback so we can log a warning once
_seen_uncategorised: set[str] = set()


def _tool_category(tool_name: str) -> str:
    """Map a tool name to its display category.

    Unknown tools land in "Other" and trigger a one-time warning so
    the developer knows to add an entry to ``_TOOL_CATEGORIES``.
    """
    cat = _TOOL_CATEGORIES.get(tool_name, _UNCATEGORISED)
    if cat is _UNCATEGORISED and tool_name not in _seen_uncategorised:
        _seen_uncategorised.add(tool_name)
        logger.warning(
            "Uncategorised tool %r — add it to _TOOL_CATEGORIES in dashboard.py",
            tool_name,
        )
    return cat


def _build_tool_table(
    records: list[dict[str, Any]],
) -> tuple[list[str], list[list[str]]]:
    """Aggregate tool_call events into grouped (headers, rows).

    Tools are sorted into categories (Drafting, Lookups, Files, …). Each
    category occupies a single row showing combined stats and the number
    of tools within it.  The category name includes the sub-tool names in
    dim text so you can see which tools are grouped without an interactive
    expand.
    """
    tool_calls = [r for r in records if r.get("event") == "tool_call"]

    # Per-tool aggregate (same as before)
    per_tool: dict[str, dict[str, float]] = {}
    for tc in tool_calls:
        tool = tc.get("tool", "unknown")
        dur = tc.get("duration_ms", 0.0) or 0.0
        if tool not in per_tool:
            per_tool[tool] = {"count": 0, "total": 0.0}
        per_tool[tool]["count"] += 1
        per_tool[tool]["total"] += dur

    # Group into categories
    categories: dict[str, dict[str, Any]] = {}
    for tool, stats in per_tool.items():
        cat = _tool_category(tool)
        if cat not in categories:
            categories[cat] = {"count": 0, "total": 0.0, "tools": {}}
        categories[cat]["count"] += stats["count"]
        categories[cat]["total"] += stats["total"]
        categories[cat]["tools"][tool] = stats

    headers = ["Tool", "Calls", "Avg (ms)", "Total (s)"]
    rows: list[list[str]] = []

    # Sort categories by _CATEGORY_ORDER, then by total time descending
    def _cat_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, float]:
        cat_name, stats = item
        try:
            order = _CATEGORY_ORDER.index(cat_name)
        except ValueError:
            order = len(_CATEGORY_ORDER)
        return (order, -stats["total"])

    for cat_name, cat_stats in sorted(categories.items(), key=_cat_sort_key):
        # Build a compact sub-tool label: "scan_files, read_file_sample, …"
        sorted_subtools = sorted(
            cat_stats["tools"].items(), key=lambda x: x[1]["total"], reverse=True
        )
        sub_labels = [f"[dim]{t}[/dim]" for t, _ in sorted_subtools]
        tool_count = len(sub_labels)
        label = f"{cat_name}  [dim]({tool_count})[/dim]"

        avg = cat_stats["total"] / cat_stats["count"] if cat_stats["count"] else 0
        total_s = cat_stats["total"] / 1000.0
        # Highlight "Other" in yellow to draw attention to uncategorised tools
        label_style = "[bold yellow]" if cat_name == _UNCATEGORISED else ""
        if label_style:
            label = f"{label_style}{cat_name}  [dim]({tool_count})[/dim][/bold yellow]"
        rows.append(
            [
                label,
                str(cat_stats["count"]),
                f"{avg:.1f}",
                f"{total_s:.2f}",
            ]
        )

    return headers, rows


def _build_node_table(
    records: list[dict[str, Any]],
) -> tuple[list[str], list[list[str]]]:
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
        rows.append(
            [
                node,
                str(stats["count"]),
                f"{avg:.1f}",
                f"{total_s:.2f}",
            ]
        )
    return headers, rows


def _build_token_summary(
    records: list[dict[str, Any]],
) -> tuple[dict[str, int | str | None], dict[str, int | str | None] | None]:
    """Aggregate token usage from ``node_end`` (model) events.

    Returns (totals, last_request) where each is a dict with keys:
        ``input_tokens``, ``output_tokens``, ``total_tokens``, ``model_name``.
    *totals* aggregates across all model calls; *last_request* is the most recent.
    """
    model_ends = [r for r in records if r.get("event") == "node_end" and r.get("node") == "model"]
    total_in = 0
    total_out = 0
    last: dict[str, int | str | None] | None = None
    for me in model_ends:
        inp = me.get("input_tokens")
        out = me.get("output_tokens")
        if inp is not None:
            total_in += int(inp)
        if out is not None:
            total_out += int(out)
        last = {
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": (inp + out) if (inp is not None and out is not None) else None,
            "model_name": me.get("model_name"),
        }
    totals = {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "total_tokens": total_in + total_out,
        "model_name": last["model_name"] if last else None,
    }
    return totals, last


def _build_token_table(
    totals: dict[str, int | str | None],
    last_request: dict[str, int | str | None] | None,
) -> Any:
    """Build a token usage table showing totals and last request."""
    from rich.table import Table

    table = Table(title="Token Usage", header_style="bold yellow")
    table.add_column("Scope")
    table.add_column("Input")
    table.add_column("Output")
    table.add_column("Total")

    total_in = totals.get("input_tokens", 0) or 0
    total_out = totals.get("output_tokens", 0) or 0
    table.add_row(
        "[dim]Cumulative[/dim]",
        str(total_in),
        str(total_out),
        str(int(total_in) + int(total_out)),
    )

    if last_request:
        li = last_request.get("input_tokens")
        lo = last_request.get("output_tokens")
        li_str = str(li) if li is not None else "—"
        lo_str = str(lo) if lo is not None else "—"
        lt = last_request.get("total_tokens")
        lt_str = str(lt) if lt is not None else "—"
        mn = last_request.get("model_name") or "—"
        table.add_row(
            f"[dim]Last request[/dim]  [cyan]{mn}[/cyan]",
            li_str,
            lo_str,
            lt_str,
        )

    return table


def _get_last_response(records: list[dict[str, Any]]) -> str | None:
    """Return the response_text from the most recent model node_end event."""
    for r in reversed(records):
        if r.get("event") == "node_end" and r.get("node") == "model":
            text = r.get("response_text")
            if text:
                return text
    return None


def _build_live_events(records: list[dict[str, Any]], max_lines: int = 25) -> list[str]:
    """Build a list of formatted event lines for the live tail.

    Enhanced to show tool arguments and results alongside timing.
    """
    recent = records[-max_lines:] if len(records) > max_lines else records
    lines = []
    for r in recent:
        ts = r.get("timestamp", "")
        evt = r.get("event", "")
        if evt == "tool_call":
            tool = r.get("tool", "?")
            dur = r.get("duration_ms")
            dur_str = f" {dur:.1f}ms" if dur is not None else ""

            # Show tool arguments (truncated from front so filename is visible)
            args_raw = r.get("args")
            args_str = ""
            if args_raw:
                preview = args_raw[-40:] if len(args_raw) > 40 else args_raw
                if len(args_raw) > 40:
                    preview = "…" + preview
                args_str = f"  [dim]args: {preview}[/dim]"

            # Show tool result (truncated from front so filename is visible)
            res_raw = r.get("result")
            res_str = ""
            if res_raw:
                res_str_val = str(res_raw)
                preview = res_str_val[-50:] if len(res_str_val) > 50 else res_str_val
                if len(res_str_val) > 50:
                    preview = "…" + preview
                res_str = f"  [cyan]→ {preview}[/cyan]"

            extra = args_str + res_str
            if extra:
                lines.append(f"{ts[11:19]}  tool_call   {tool}{dur_str}{extra}")
            else:
                lines.append(f"{ts[11:19]}  tool_call   {tool}{dur_str}")

        elif evt == "node_start":
            node = r.get("node", "?")
            lines.append(f"{ts[11:19]}  node_start  {node}")
        elif evt == "node_end":
            node = r.get("node", "?")
            dur = r.get("duration_ms")
            dur_str = f" {dur:.1f}ms" if dur is not None else ""
            # Append token info when available for model nodes
            inp = r.get("input_tokens")
            out = r.get("output_tokens")
            tok_str = ""
            if inp is not None and out is not None:
                tok_str = f"  [yellow]Δ{inp}→{out}[/yellow]"
            elif inp is not None:
                tok_str = f"  [yellow]Δ{inp} in[/yellow]"
            elif out is not None:
                tok_str = f"  [yellow]Δ{out} out[/yellow]"

            # Append response preview for model nodes
            resp = r.get("response_text")
            resp_str = ""
            if resp:
                # Show first line (or truncated first ~60 chars)
                preview = resp.split("\n")[0][:60]
                if len(resp) > 60 or "\n" in resp:
                    preview += "…"
                resp_str = f'  [green]"{preview}"[/green]'

            extra = tok_str + resp_str
            if extra:
                lines.append(f"{ts[11:19]}  node_end    {node}{dur_str}{extra}")
            else:
                lines.append(f"{ts[11:19]}  node_end    {node}{dur_str}")
        else:
            lines.append(f"{ts[11:19]}  {evt}")
    return lines


def _build_conversation_flow(records: list[dict[str, Any]], max_steps: int = 8) -> list[str]:
    """Reconstruct the AgentState message flow from profiling events.

    Walks the record list in chronological order and emits lines for each
    round-trip: ``user → model → tools → model → user → …``.

    - ``user`` prompts are extracted from ``node_end`` model events that had
      a large ``messages_in`` count (first message in the batch is the user).
    - ``model`` replies come from ``node_end`` model ``response_text``.
    - ``tool_calls`` show which tools the model asked to run, with args.
    - ``tool results`` show the return values from ``tool_call`` ``result``.

    Returns up to *max_steps* lines, newest last.
    """
    flow: list[str] = []
    # Walk forward to build a timeline of user → AI → tool round-trips
    for r in records:
        evt = r.get("event", "")
        ts = r.get("timestamp", "")
        time_str = ts[11:19] if ts else ""

        if evt == "node_end" and r.get("node") == "model":
            resp = r.get("response_text")
            if resp:
                preview = resp.split("\n")[0][:80]
                if len(resp) > 80 or "\n" in resp:
                    preview += "…"
                flow.append(f"[{time_str}] [bold green]AI:[/bold green] {preview}")

            # Show what tools the model chose to call
            produced = r.get("produced_tool_calls", False)
            if produced:
                # Look ahead for the tool_call events that follow
                flow.append(f"[{time_str}] [yellow]→ requesting tools…[/yellow]")

        elif evt == "tool_call":
            tool = r.get("tool", "?")
            dur = r.get("duration_ms")
            dur_str = f" ({dur:.0f}ms)" if dur is not None else ""

            args_raw = r.get("args", "")
            args_preview = ""
            if args_raw:
                args_preview = args_raw[-50:] if len(args_raw) > 50 else args_raw
                if len(args_raw) > 50:
                    args_preview = "…" + args_preview

            res_raw = r.get("result")
            res_str = ""
            if res_raw:
                res_str_val = str(res_raw)
                res_preview = res_str_val[-60:] if len(res_str_val) > 60 else res_str_val
                if len(res_str_val) > 60:
                    res_preview = "…" + res_preview
                res_str = f" → [cyan]{res_preview}[/cyan]"

            args_part = f" args: {args_preview}" if args_preview else ""
            flow.append(f"[{time_str}] [magenta]⚡ {tool}[/magenta]{dur_str}{args_part}{res_str}")

    # Keep only the most recent steps
    if len(flow) > max_steps:
        flow = flow[-max_steps:]
    return flow


class _NoDataPanel:
    """Placeholder rendered when there are no records."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def __rich__(self) -> str:
        sid = self._session_id
        return f"[bold yellow]Session:[/] {sid}\n\n[yellow]No profiling data available yet.[/]"


def format_session_summary(session_id: str, records: list[dict[str, Any]]) -> Any:
    """Build a Rich renderable summarising the profiler data.

    Returns a ``rich.layout.Layout`` with tool timing and node timing tables
    plus a live event tail. If *records* is empty, returns a placeholder
    layout saying no data is available.
    """
    from rich.console import Group
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )

    # Header
    now = _config.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    header_text = Text()
    header_text.append(" Agent Profiler Dashboard", style="bold cyan")
    header_text.append(f"  |  Session: {session_id}", style="cyan")
    layout["header"].update(Panel(header_text, style="cyan"))

    if not records:
        layout["body"].update(_NoDataPanel(session_id))
        layout["footer"].update(Text(f"Last refresh: {now}", style="dim"))
        return layout

    # Load CrateState
    crate_state = _load_cratestate(session_id)
    crate_panel = _build_cratestate_panel(crate_state)

    # Build tables
    tool_headers, tool_rows = _build_tool_table(records)
    # Tool rows are grouped into ~10 categories so no capping needed.
    # Split into two side-by-side tables to use horizontal space better.
    half = (len(tool_rows) + 1) // 2
    tool_table_left = Table(title="Tool Call Times", header_style="bold magenta")
    for h in tool_headers:
        tool_table_left.add_column(h)
    for row in tool_rows[:half]:
        tool_table_left.add_row(*row)

    tool_table_right = Table(title="", header_style="bold magenta", show_header=False)
    for h in tool_headers:
        tool_table_right.add_column(h)
    for row in tool_rows[half:]:
        tool_table_right.add_row(*row)

    node_headers, node_rows = _build_node_table(records)
    token_totals, token_last = _build_token_summary(records)
    last_response = _get_last_response(records)
    live_lines = _build_live_events(records)

    node_table = Table(title="Node Timings", header_style="bold green")
    for h in node_headers:
        node_table.add_column(h)
    for row in node_rows:
        node_table.add_row(*row)

    token_table = _build_token_table(token_totals, token_last)

    live_panel = Panel(
        "\n".join(live_lines[-10:]),
        title="Recent Events",
        border_style="dim",
    )

    # Conversation flow panel — shows the AgentState message round-trips
    conversation_lines = _build_conversation_flow(records)
    conversation_panel = Panel(
        "\n".join(conversation_lines),
        title="Conversation Flow (AgentState)",
        border_style="magenta",
        highlight=True,
    )

    response_panel = None
    if last_response:
        # Truncate displayed text in the panel to avoid overwhelming the layout
        display = last_response[:500]
        if len(last_response) > 500:
            display += "\n[dim]… (truncated)[/dim]"
        response_panel = Panel(
            display,
            title="Last Agent Response",
            border_style="green",
            highlight=True,
        )

    # Combine into body — tool table goes last so it doesn't push live feeds off-screen
    from rich.columns import Columns

    body_parts = [
        Columns([crate_panel, token_table, node_table], equal=True, expand=True),
    ]
    if response_panel:
        body_parts.append(response_panel)
    body_parts.append(live_panel)
    body_parts.append(conversation_panel)
    # Tool table at the bottom — it accumulates rows and would push everything else down
    body_parts.append(Columns([tool_table_left, tool_table_right], equal=True, expand=True))
    body = Group(*body_parts)
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
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live

    console = Console()
    session_path = profile_path.parent
    crate_state_path = session_path / "crate_state.json"
    last_mtime: float = 0.0
    last_crate_mtime: float = 0.0

    def _build() -> Layout:
        nonlocal last_mtime, last_crate_mtime
        # Re-read profile only if changed (avoid pointless I/O)
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

    with Live(
        _build(), console=console, refresh_per_second=1 / refresh_interval, screen=False
    ) as live:
        # watchfiles step is in milliseconds; convert from our seconds-based interval
        step_ms = int(refresh_interval * 1000)
        # Watch both profile.ndjson and crate_state.json so CrateState updates too
        watch_paths = [str(profile_path)]
        if crate_state_path.exists():
            watch_paths.append(str(crate_state_path))
        for _changes in watch(*watch_paths, step=step_ms):
            live.update(_build())
