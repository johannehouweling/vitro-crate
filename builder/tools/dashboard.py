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

SESSION_DIR = _config.session_root()

# ---------------------------------------------------------------------------
# Agent status (▶ / ⏸ / ⏹) — issue #193
# ---------------------------------------------------------------------------
# The dashboard surfaces, at a glance, whether the agent is *driving* (working)
# or *waiting* on the human. The classification is a pure function of the
# profiler records so it is trivially testable (see tests/test_dashboard_status.py).

STATUS_DRIVING = "driving"  # ▶ a tool/node is in progress; the loop is moving
STATUS_WAITING = "waiting"  # ⏸ blocked on a human (pending HITL call)
STATUS_IDLE = "idle"  # ⏹ no activity / a terminal end

# ---------------------------------------------------------------------------
# Dashboard palette — one coherent, semantic colour scheme
# ---------------------------------------------------------------------------
# Colour carries *meaning*, never decoration:
#   * structure (panel headers, section/field labels, borders) is uniform —
#     one header style, one border style, one dim style for labels/units;
#   * a single accent highlights emphasised values;
#   * green/yellow/red are reserved strictly for status (pass / warn / fail).
# Use these constants instead of ad-hoc ``bold yellow`` / ``magenta`` / ``blue``.

HEADER_STYLE = "bold cyan"  # panel titles + field/section labels (structure)
BORDER_STYLE = "cyan"  # every data-panel border (uniform, neutral)
LABEL_STYLE = "dim"  # secondary text: units, counts, parentheticals
ACCENT_STYLE = "bold cyan"  # an emphasised value (e.g. the last-called tool)
OK_STYLE = "green"  # semantic: a check passed / a healthy score
WARN_STYLE = "yellow"  # semantic: a soft warning / a middling score
ERR_STYLE = "red"  # semantic: a failure / a blocking issue / stuck

# HITL tools that block the agent loop on a real person. When the engine is
# about to call one of these it emits a ``hitl_wait`` event *before* blocking
# (see builder/engine.py run_tool); the matching ``tool_call`` event is only
# written *after* the human responds, so a pending wait is observable as a
# trailing ``hitl_wait`` with no following ``tool_call`` for the same tool.
_HITL_TOOLS = frozenset({"present_to_human", "request_input"})


def determine_agent_status(records: list[dict[str, Any]]) -> str:
    """Classify the agent's live status from profiler *records*.

    Returns one of :data:`STATUS_DRIVING`, :data:`STATUS_WAITING`, or
    :data:`STATUS_IDLE`.

    Rules (evaluated in priority order):

    * **waiting** — the agent is blocked on a human: the most recent
      ``hitl_wait`` event has no matching ``tool_call`` for the same HITL tool
      after it. A pending HITL call is *not* otherwise observable, because the
      profiler logs a tool's ``tool_call`` only after it returns — i.e. after
      the human has already responded. The engine therefore emits an explicit
      ``hitl_wait`` marker before blocking (see builder/engine.py).
    * **driving** — a graph node has started but not ended (more ``node_start``
      than ``node_end`` events), or the latest event is mid-flight activity
      (a completed ``tool_call`` with no closing ``node_end`` yet).
    * **idle** — no records at all, or the loop has settled on a terminal
      ``node_end`` with nothing pending.

    The function never raises on malformed records; unknown events are ignored.
    """
    if not records:
        return STATUS_IDLE

    # 1. Pending HITL? Scan from the end for the latest hitl_wait and check
    #    whether a matching tool_call landed afterwards (= human responded).
    for idx in range(len(records) - 1, -1, -1):
        rec = records[idx]
        if rec.get("event") != "hitl_wait":
            continue
        tool = rec.get("tool")
        resolved = any(
            later.get("event") == "tool_call" and later.get("tool") == tool
            for later in records[idx + 1 :]
        )
        return STATUS_DRIVING if resolved else STATUS_WAITING

    # 2. An open graph node (started, not yet ended) means work is in flight.
    starts = sum(1 for r in records if r.get("event") == "node_start")
    ends = sum(1 for r in records if r.get("event") == "node_end")
    if starts > ends:
        return STATUS_DRIVING

    # 3. The latest meaningful event decides between mid-flight and settled.
    last_event = records[-1].get("event")
    if last_event == "tool_call":
        return STATUS_DRIVING
    return STATUS_IDLE


def _status_badge(status: str) -> tuple[str, str, str]:
    """Map an agent status to its ``(symbol, label, rich_style)`` triple.

    Colours come from the shared palette so the badge reads with the same
    semantics as the rest of the dashboard: driving is healthy (green),
    waiting is a soft "needs you" warning (yellow), idle is muted (dim).
    """
    if status == STATUS_DRIVING:
        return "▶", "driving", f"bold {OK_STYLE}"
    if status == STATUS_WAITING:
        return "⏸", "awaiting input", f"bold {WARN_STYLE}"
    return "⏹", "idle", LABEL_STYLE


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


def format_mit_coverage(
    overall_score: float | None, *, assessed: bool
) -> tuple[str, str]:
    """Format an MIT coverage score for display — the single source of truth
    shared by the profiler dashboard panel and the interactive UI
    (:mod:`builder.agents.ui`) so both build arms render coverage identically
    (issue #355).

    ``overall_score`` is a fraction in ``[0.0, 1.0]`` (see
    :class:`builder.state.MITReport`). Returns ``(text, rich_color)``: the text
    is a whole-percent string (e.g. ``"85%"``) and the colour tracks the score
    on the fraction scale — green ``>= 0.8``, yellow ``>= 0.5``, else red. When
    the crate was never assessed (``assessed`` false, e.g. the default empty
    report), returns ``("—", "dim")`` so an unscored crate is not shown as a
    misleading red 0%.
    """
    if not assessed or overall_score is None:
        return "—", LABEL_STYLE
    pct = round(overall_score * 100)
    if overall_score >= 0.8:
        color = OK_STYLE
    elif overall_score >= 0.5:
        color = WARN_STYLE
    else:
        color = ERR_STYLE
    return f"{pct}%", color


def _build_cratestate_panel(
    state: dict[str, Any] | None,
    status: str = STATUS_IDLE,
) -> Any:
    """Build a Rich Panel summarising CrateState (single-line, compact).

    Shows phase, entity counts, validation status, MIT score, iteration count.
    The panel title carries the ▶/⏸/⏹ agent-status badge (issue #193) so the
    live and static dashboards both signal whether the agent is driving or
    awaiting human input. Returns a placeholder Panel when *state* is None.
    """
    from rich.panel import Panel
    from rich.text import Text

    symbol, label, badge_style = _status_badge(status)
    title = Text.assemble(
        (f"{symbol} {label}", badge_style),
        ("  │  CrateState Overview", HEADER_STYLE),
    )

    if state is None:
        return Panel(
            f"[{LABEL_STYLE}]No CrateState data available.[/{LABEL_STYLE}]",
            title=title,
            border_style=BORDER_STYLE,
        )

    # Phase — colour tracks progress: muted early, accent mid-flight, ok/err at ends.
    phase = _determine_phase_from_state(state)
    phase_colors = {
        "initial": LABEL_STYLE,
        "scanning": WARN_STYLE,
        "drafting": ACCENT_STYLE,
        "validating": ACCENT_STYLE,
        "complete": OK_STYLE,
        "stuck": ERR_STYLE,
    }
    pc = phase_colors.get(phase, "white")

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
    entity_parts = [f"{n}={c}" for n, c in entity_counts[:6]]
    if len(entity_counts) > 6:
        entity_parts.append(f"...+{total - sum(c for _, c in entity_counts[:6])}")

    # Validation status
    val = state.get("validation", {})
    base_ok = val.get("base_passed", False)
    isa_ok = val.get("isa_passed", False)
    tox_ok = val.get("tox_passed", False)
    required_count = len(val.get("required_issues", []))
    val_parts = [
        f"{'✓' if base_ok else '✗'} Base",
        f"{'✓' if isa_ok else '✗'} ISA",
        f"{'✓' if tox_ok else '✗'} Tox",
    ]
    if base_ok and isa_ok and tox_ok:
        val_color = OK_STYLE
    elif required_count > 0:
        val_color = ERR_STYLE
    else:
        val_color = WARN_STYLE
    val_str = " ".join(val_parts)
    if required_count > 0:
        val_str += f"  [{ERR_STYLE}]{required_count} REQUIRED[/{ERR_STYLE}]"

    # MIT score — shared formatter keeps this identical to the interactive UI.
    mit = state.get("mit_assessment", {})
    mit_text, mit_color = format_mit_coverage(
        mit.get("overall_score"),
        assessed=bool(mit.get("module_scores")),
    )

    # Iteration
    iteration = state.get("iteration_count", 0)
    stuck = state.get("stuck", False)

    # One-line summary — all on a single rich Text
    text = Text.assemble(
        ("Phase: ", HEADER_STYLE),
        (phase, pc),
        ("  │  Entities: ", HEADER_STYLE),
        (f"{total}  ({', '.join(f'{n}={c}' for n, c in entity_counts[:3])})", ""),
        ("  │  Validation: ", HEADER_STYLE),
        (val_str, val_color),
        ("  │  MIT: ", HEADER_STYLE),
        (mit_text, mit_color),
        ("  │  Iteration: ", HEADER_STYLE),
        (f"{iteration}{' ⚠ STUCK' if stuck else ''}", ERR_STYLE if stuck else "white"),
    )

    return Panel(text, title=title, border_style=BORDER_STYLE)


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

# Emoji icons per tool for compact display
_TOOL_ICONS: dict[str, str] = {
    "draft_investigation": "\U0001f4cb",
    "draft_study": "\U0001f4cb",
    "draft_assay": "\U0001f4cb",
    "draft_process": "\U0001f4cb",
    "draft_protocol": "\U0001f4cb",
    "draft_sample": "\U0001f4cb",
    "draft_molecular_entity": "\U0001f4cb",
    "draft_cell_line_sample": "\U0001f4cb",
    "draft_person": "\U0001f4cb",
    "draft_organization": "\U0001f4cb",
    "draft_publication": "\U0001f4cb",
    "draft_defined_term": "\U0001f4cb",
    "draft_property_value": "\U0001f4cb",
    "draft_file": "\U0001f4cb",
    "list_entities": "\U0001f4cb",
    "list_scanned_files": "\U0001f4c2",
    "remove_entity": "\U0001f4cb",
    "set_fields": "\U0001f4dd",
    "link": "\U0001f517",
    "attach_files": "\U0001f4ce",
    "check_provenance": "\U0001f9ec",
    "lookup_compound": "\U0001f50d",
    "lookup_cell_line": "\U0001f50d",
    "lookup_cell_line_by_name": "\U0001f50d",
    "lookup_aop": "\U0001f50d",
    "lookup_bao_term": "\U0001f50d",
    "lookup_ontology_term": "\U0001f50d",
    "lookup_unit": "\U0001f50d",
    "lookup_dtxsid": "\U0001f50d",
    "lookup_orcid": "\U0001f50d",
    "lookup_ror": "\U0001f50d",
    "lookup_doi": "\U0001f50d",
    "scan_files": "\U0001f4c2",
    "read_file_sample": "\U0001f4c2",
    "read_multiple_files": "\U0001f4c2",
    "preview_archive": "\U0001f4c2",
    "extract_pdf_text": "\U0001f4c4",
    "extract_pdf_tables": "\U0001f4c4",
    "verify_identifier": "\u2705",
    "verify_all_identifiers": "\u2705",
    "build_and_validate": "\u2714\ufe0f",
    "fix_required_issues": "\U0001f527",
    "export_crate": "\U0001f3ed",
    "build_crate": "\U0001f3ed",
    "validate": "\u2714\ufe0f",
    "validate_table": "\U0001f4c8",
    "populate_condition_table": "\U0001f4c8",
    "assess_mit_coverage": "\U0001f52e",
    "assess_fair_maturity": "\U0001f52e",
    "save_session": "\U0001f4be",
    "load_session": "\U0001f4c1",
    "list_sessions": "\U0001f4ca",
    "get_status": "\U0001f4ac",
    "get_hint": "\U0001f4a1",
    "present_to_human": "\U0001f468\u200d\U0001f4bb",
    "request_input": "\u2328\ufe0f",
}

# Category ordering for display groups
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


def _build_tool_lines(
    records: list[dict[str, Any]],
    last_tool_name: str = "",
) -> str:
    """Build a compact single-line tool summary with emoji icons.

    Tools are sorted by total time descending, displayed with
    category emoji icons.  The last-called tool is highlighted
    in cyan.  Tools are separated by `` │ `` pipes for a sleek
    look.

    Format::
        📋 validate (27) ⏱15s/∑398s │ 🔍 lookup_compound (6) ⏱0.8s/∑5.0s │ …
    """
    tool_calls = [r for r in records if r.get("event") == "tool_call"]

    per_tool: dict[str, dict[str, float]] = {}
    for tc in tool_calls:
        tool = tc.get("tool", "unknown")
        dur = tc.get("duration_ms", 0.0) or 0.0
        if tool not in per_tool:
            per_tool[tool] = {"count": 0, "total": 0.0}
        per_tool[tool]["count"] += 1
        per_tool[tool]["total"] += dur

    sorted_tools = sorted(per_tool.items(), key=lambda x: x[1]["total"], reverse=True)

    parts: list[str] = []
    for tool, stats in sorted_tools:
        count = stats["count"]
        total_ms = stats["total"]
        avg_ms = total_ms / count if count else 0
        icon = _TOOL_ICONS.get(tool, "\u2699\ufe0f")
        is_last = tool == last_tool_name
        style = ACCENT_STYLE if is_last else ""
        open_tag = f"[{style}]" if style else ""
        close_tag = f"[/{style}]" if style else ""

        # Format times: show whole seconds if >= 1000ms, else ms
        if total_ms >= 1000:
            total_s = total_ms / 1000.0
            if avg_ms >= 1000:
                avg_s = avg_ms / 1000.0
                time_part = f"{avg_s:.1f}s/{total_s:.1f}s"
            else:
                time_part = f"{avg_ms:.0f}ms/{total_s:.1f}s"
        else:
            time_part = f"{avg_ms:.0f}ms/{total_ms:.0f}ms"

        parts.append(
            f"{open_tag}{icon} {tool}"
            f"[{LABEL_STYLE}] ({count}) {time_part}[/{LABEL_STYLE}]{close_tag}"
        )

    return " \u2502 ".join(parts)


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

    table = Table(title="Token Usage", header_style=HEADER_STYLE)
    table.add_column("Scope")
    table.add_column("Input")
    table.add_column("Output")
    table.add_column("Total")

    total_in = totals.get("input_tokens", 0) or 0
    total_out = totals.get("output_tokens", 0) or 0
    table.add_row(
        f"[{LABEL_STYLE}]Cumulative[/{LABEL_STYLE}]",
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
            f"[{LABEL_STYLE}]Last request[/{LABEL_STYLE}]  [{ACCENT_STYLE}]{mn}[/{ACCENT_STYLE}]",
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
                args_str = f"  [{LABEL_STYLE}]args: {preview}[/{LABEL_STYLE}]"

            # Show tool result (truncated from front so filename is visible)
            res_raw = r.get("result")
            res_str = ""
            if res_raw:
                res_str_val = str(res_raw)
                preview = res_str_val[-50:] if len(res_str_val) > 50 else res_str_val
                if len(res_str_val) > 50:
                    preview = "…" + preview
                res_str = f"  [{ACCENT_STYLE}]→ {preview}[/{ACCENT_STYLE}]"

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
                tok_str = f"  [{LABEL_STYLE}]Δ{inp}→{out}[/{LABEL_STYLE}]"
            elif inp is not None:
                tok_str = f"  [{LABEL_STYLE}]Δ{inp} in[/{LABEL_STYLE}]"
            elif out is not None:
                tok_str = f"  [{LABEL_STYLE}]Δ{out} out[/{LABEL_STYLE}]"

            # Append response preview for model nodes
            resp = r.get("response_text")
            resp_str = ""
            if resp:
                # Show first line (or truncated first ~60 chars)
                preview = resp.split("\n")[0][:60]
                if len(resp) > 60 or "\n" in resp:
                    preview += "…"
                resp_str = f'  [{ACCENT_STYLE}]"{preview}"[/{ACCENT_STYLE}]'

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
                flow.append(f"[{time_str}] [{HEADER_STYLE}]AI:[/{HEADER_STYLE}] {preview}")

            # Show what tools the model chose to call
            produced = r.get("produced_tool_calls", False)
            if produced:
                # Look ahead for the tool_call events that follow
                flow.append(f"[{time_str}] [{LABEL_STYLE}]→ requesting tools…[/{LABEL_STYLE}]")

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
                res_str = f" → [{LABEL_STYLE}]{res_preview}[/{LABEL_STYLE}]"

            args_part = f" args: {args_preview}" if args_preview else ""
            flow.append(
                f"[{time_str}] [{ACCENT_STYLE}]⚡ {tool}[/{ACCENT_STYLE}]"
                f"{dur_str}{args_part}{res_str}"
            )

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
        return (
            f"[{HEADER_STYLE}]Session:[/] {sid}\n\n"
            f"[{LABEL_STYLE}]No profiling data available yet.[/]"
        )


def format_session_summary(session_id: str, records: list[dict[str, Any]]) -> Any:
    """Build a Rich renderable summarising the profiler data.

    Returns a ``rich.layout.Layout`` with tool timing and node timing tables
    plus a live event tail. If *records* is empty, returns a placeholder
    layout saying no data is available.
    """
    from rich.console import Group
    from rich.layout import Layout
    from rich.panel import Panel
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
    header_text.append(" Agent Profiler Dashboard", style=HEADER_STYLE)
    header_text.append(f"  |  Session: {session_id}", style=BORDER_STYLE)
    layout["header"].update(Panel(header_text, style=BORDER_STYLE))

    if not records:
        layout["body"].update(_NoDataPanel(session_id))
        layout["footer"].update(Text(f"Last refresh: {now}", style=LABEL_STYLE))
        return layout

    # Load CrateState
    crate_state = _load_cratestate(session_id)
    # Agent status (▶ driving / ⏸ awaiting input / ⏹ idle) — issue #193.
    agent_status = determine_agent_status(records)
    crate_panel = _build_cratestate_panel(crate_state, status=agent_status)

    # Build tables
    # Determine the last tool called for highlighting
    tool_calls_events = [r for r in records if r.get("event") == "tool_call"]
    last_tool_name = tool_calls_events[-1].get("tool", "") if tool_calls_events else ""

    tool_lines = _build_tool_lines(records, last_tool_name=last_tool_name)

    node_headers, node_rows = _build_node_table(records)
    token_totals, token_last = _build_token_summary(records)
    last_response = _get_last_response(records)

    # Token usage + Node timings — inline text, no tables
    tok_in = token_totals.get("input_tokens", 0) or 0
    tok_out = token_totals.get("output_tokens", 0) or 0
    tok_total = int(tok_in) + int(tok_out)
    last_in = (token_last or {}).get("input_tokens")
    last_out = (token_last or {}).get("output_tokens")
    last_model = (token_last or {}).get("model_name") or ""
    last_str = ""
    if last_in is not None:
        last_total = int(last_in) + int(last_out or 0)
        last_str = f"  · last {last_model}: {last_in}→{last_out} ({last_total})"

    # Compute costs — use get_model_provider for the model-specific vendor prefix
    from builder.config import get_model_provider
    from builder.pricing import compute_cost, format_cost

    model_provider = get_model_provider()
    cumulative_cost_info = compute_cost(
        int(tok_in), int(tok_out), str(last_model), provider=model_provider
    )
    cost_str = ""
    if cumulative_cost_info.get("total_cost") is not None:
        cost_str = f"  · est {format_cost(cumulative_cost_info['total_cost'])}"

    node_parts = []
    for node, calls, avg, total in node_rows:
        node_parts.append(f"[bold]{node}[/bold] {calls}× {avg}ms  {total}s")
    node_str = f"  [{LABEL_STYLE}]│[/{LABEL_STYLE}]  ".join(node_parts)

    from rich.text import Text as RichText

    summary_text = RichText.from_markup(
        f"[{HEADER_STYLE}]Token Usage[/{HEADER_STYLE}]: cumulative {tok_in}→{tok_out} ({tok_total})"
        f"[{LABEL_STYLE}]{last_str}[/{LABEL_STYLE}]"
        f"[{LABEL_STYLE}]{cost_str}[/{LABEL_STYLE}]"
        f"  [{LABEL_STYLE}]║[/{LABEL_STYLE}]  "
        f"[{HEADER_STYLE}]Node Timings[/{HEADER_STYLE}]:  {node_str}"
    )

    # Conversation flow panel — shows the AgentState message round-trips
    conversation_lines = _build_conversation_flow(records)
    conversation_panel = Panel(
        "\n".join(conversation_lines),
        title="Conversation Flow (AgentState)",
        border_style=BORDER_STYLE,
        highlight=True,
    )

    response_panel = None
    if last_response:
        # Show only the first line — keeps the panel compact
        display = last_response.split("\n")[0][:500]
        response_panel = Panel(
            display,
            title="Last Agent Response",
            border_style=BORDER_STYLE,
            highlight=True,
        )

    # Combine into body — tool lines at the bottom. Every data panel uses the
    # one neutral border so colour stays reserved for meaning, not decoration.
    body_parts = [
        crate_panel,
        Panel(summary_text, border_style=BORDER_STYLE, padding=(0, 0)),
    ]
    if response_panel:
        body_parts.append(response_panel)
    body_parts.append(conversation_panel)
    # Tool lines at the bottom — compact one-line per tool summary
    tool_lines_wrapper = Panel(
        tool_lines if tool_lines else f"[{LABEL_STYLE}]no tool calls yet[/{LABEL_STYLE}]",
        title="Tool Call Times",
        border_style=BORDER_STYLE,
        padding=(0, 0),
    )
    body_parts.append(tool_lines_wrapper)
    body = Group(*body_parts)
    layout["body"].update(body)
    layout["footer"].update(Text(f"Last refresh: {now}", style=LABEL_STYLE))

    return layout


# ---------------------------------------------------------------------------
# Live dashboard (TUI)
# ---------------------------------------------------------------------------

# watchfiles' ``step`` is a debounce *quiet-period* (ms): changes are only
# yielded once the filesystem has been quiet for this long. It must stay small
# (the watchfiles default) so a steady stream of profiler writes is surfaced
# promptly — it is NOT the render cadence. Deriving it from ``refresh_interval``
# (2000ms) starved the watch loop, so the dashboard only updated on load (#121).
# The user-facing refresh rate is Rich ``Live(refresh_per_second=...)`` instead.
_WATCH_STEP_MS = 50


def _read_records_cached(
    profile_path: Path, last_mtime: float, cached: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], float]:
    """Return ``(records, mtime)``, re-reading ``profile.ndjson`` only when its
    mtime changed; otherwise reuse *cached*.

    Reusing the cache matters when the watch loop fires for a *crate_state.json*
    change while ``profile.ndjson`` is unchanged: returning ``[]`` there would
    blank the profile panel to the "no data" placeholder on every CrateState
    update (#121).
    """
    try:
        current_mtime = profile_path.stat().st_mtime
    except OSError:
        return cached, last_mtime
    if current_mtime != last_mtime:
        return read_profile(profile_path), current_mtime
    return cached, current_mtime


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

    If *session_id* is None, the dashboard *follows the newest session*: it
    re-resolves the most recent session on every refresh, so a fresh
    ``--interactive`` run is picked up live with no restart (#267). If an
    explicit *session_id* is given, the dashboard stays pinned to it.

    The display refreshes on every filesystem change under :data:`SESSION_DIR`
    AND at least every *refresh_interval* seconds even with no events (a poll
    fallback that is robust against FSEvents/atomic-save quirks).

    Press Ctrl+C to exit.
    """
    if session_id is not None:
        sessions = list_sessions_available()
        if not any(s["session_id"] == session_id for s in sessions):
            print(f"Session not found: {session_id}")
            return

    try:
        _run_live_dashboard(session_id=session_id, refresh_interval=refresh_interval)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nDashboard closed.")


def _run_live_dashboard(
    session_id: str | None,
    refresh_interval: float,
) -> None:
    """Inner live-dashboard loop with Rich ``Live`` display and file-watching.

    Watches the :data:`SESSION_DIR` *root* (not an individual session/file) and
    rebuilds + renders on **every wake** — both real change events and the
    periodic timeout wake (``yield_on_timeout``). This is the #267 fix:

    * The previous basename filter (``_change_touches``) discarded the atomic
      save signal. ``save_session`` writes ``crate_state.json`` via tempfile +
      ``os.replace``; on macOS the change batch contains ONLY the temp file
      (``.crate_state_tmp_*``), which the filter excluded — so the render never
      fired. Rendering on every wake (event OR timeout) removes that whole
      failure mode.
    * The session was pinned at startup. With *session_id* None we re-resolve
      the newest session via :func:`list_sessions_available` on every wake, so a
      new run is followed live. An explicit *session_id* stays pinned.

    The ``profile.ndjson`` mtime cache (:func:`_read_records_cached`) is kept so
    the profile isn't needlessly re-parsed; ``crate_state.json`` is re-read each
    render (via :func:`format_session_summary` -> :func:`_load_cratestate`).
    """
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live

    console = Console()

    # Per-session mtime cache for profile.ndjson. Keyed by session_id so that,
    # when following the newest session, switching sessions doesn't reuse a stale
    # cache from a different run.
    cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def _resolve_session() -> str | None:
        """Pick the session to render this wake: pinned, or the newest one.

        Do not call ``list_sessions_available`` here: that helper parses every
        ``profile.ndjson`` under ``sessions/`` to build static-session metadata.
        The live dashboard only needs the newest directory, and reparsing hundreds
        of historical profiles every two seconds makes the TUI appear frozen.
        """
        if session_id is not None:
            return session_id
        try:
            newest = max(
                (
                    child
                    for child in SESSION_DIR.iterdir()
                    if child.is_dir() and (child / "profile.ndjson").is_file()
                ),
                key=lambda child: child.stat().st_mtime,
                default=None,
            )
        except OSError:
            return None
        return newest.name if newest is not None else None

    def _build() -> Layout:
        sid = _resolve_session()
        if sid is None:
            # No sessions yet — render an empty placeholder and keep polling.
            return format_session_summary("(waiting for a session…)", [])
        profile_path = SESSION_DIR / sid / "profile.ndjson"
        last_mtime, records = cache.get(sid, (0.0, []))
        records, last_mtime = _read_records_cached(profile_path, last_mtime, records)
        cache[sid] = (last_mtime, records)
        return format_session_summary(sid, records)

    try:
        from watchfiles import watch
    except ImportError:
        logger.warning(
            "watchfiles not installed — dashboard will show a static snapshot. "
            "Install with: uv add watchfiles"
        )
        console.print(_build())
        return

    # SESSION_DIR may not exist yet (no run has written a session). watchfiles
    # cannot watch a missing path, so create the root; per-session dirs appear
    # under it as runs start and the follow-newest loop picks them up.
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Could not create session dir %s", SESSION_DIR)

    with Live(
        _build(), console=console, refresh_per_second=1 / refresh_interval, screen=False
    ) as live:
        # Watch the SESSION_DIR ROOT with an event-OR-timeout loop:
        #   * ``yield_on_timeout`` + ``rust_timeout`` wake the loop at least every
        #     ``refresh_interval`` even with zero FS events (poll fallback);
        #   * any real change also wakes it immediately.
        # On every wake we rebuild + render unconditionally — no path filtering,
        # so atomic-save temp churn can no longer hide the update (#267).
        # ``step`` stays small (watchfiles' debounce quiet-period) so a steady
        # stream of writes is surfaced promptly.
        for _changes in watch(
            str(SESSION_DIR),
            step=_WATCH_STEP_MS,
            rust_timeout=int(refresh_interval * 1000),
            yield_on_timeout=True,
        ):
            live.update(_build())
