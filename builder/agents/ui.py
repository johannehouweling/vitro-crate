"""Shared interactive terminal UI for both build arms (pipeline + ReAct).

Single source of truth for the interactive chrome — the shared Rich
``Console``, the one-line status bar, agent-reply rendering, the resume
summary and goodbye panels, and the boxed ``❯`` prompt — so the
deterministic pipeline and the legacy ReAct arm render *identically*.
Both arms import from here; neither keeps a private copy (harmonization,
GitHub #344).

Design contract:

* The ``render_*`` functions are **pure** — each takes a :class:`UiSnapshot`
  (or plain values) and returns a Rich renderable, so they unit-test without
  a TTY or a live engine.
* :func:`snapshot_from_engine` is the single impure adapter: it reads engine
  state and, best-effort, ``profile.ndjson`` for cumulative token/cost totals.
* This module imports ``rich`` / ``prompt_toolkit`` / ``builder.pricing`` /
  ``builder.config`` and **neither build arm** — depending on an arm here
  would create an ``agents → agents`` import cycle. ``engine`` is duck-typed
  through :func:`snapshot_from_engine`'s parameter, never imported.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from builder.engine import AgentEngine

logger = logging.getLogger(__name__)

# Marker glyphs shared across the chrome (green ● = active/pass, ○ = pending).
_PASS_DOT = "[green]●[/green]"
_PENDING_DOT = "[grey50]○[/grey50]"
_SEP = "[grey42]·[/grey42]"

_console: Console | None = None


def get_console() -> Console:
    """Return the process-wide shared Rich :class:`Console` (memoized).

    Both arms render through one console so styling, width, and the
    console-animation registry stay consistent.
    """
    global _console
    if _console is None:
        _console = Console()
    return _console


# ---------------------------------------------------------------------------
# Message flattening — the #341 raw-message-leak fix
# ---------------------------------------------------------------------------


def flatten_message_content(content: Any) -> str:
    """Reduce an LLM message ``content`` to human-readable text.

    Model replies may arrive as a plain string or as a list of content
    blocks — dicts like ``{"type": "text", "text": ...}`` (plus ``annotations``
    / ``id`` / ``phase`` metadata) or block objects exposing a ``.text``
    attribute. Printing such a list with ``str()`` leaks the Python repr to
    the terminal (GitHub #341). This joins the ``text`` of every text block
    and drops non-text blocks (``tool_use``, ``thinking``, …) and metadata.

    Args:
        content: A message ``content`` value (``str``, ``None``, or a list of
            string / dict / block-object items).

    Returns:
        The concatenated human-readable text (empty string when there is none).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            else:
                text = getattr(block, "text", None)
                if isinstance(text, str) and getattr(block, "type", "text") == "text":
                    parts.append(text)
        return "".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# UiSnapshot — the pure inputs the renderers need
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UiSnapshot:
    """A flat, render-ready view of session state.

    Decoupling the renderers from the live engine keeps them pure and
    unit-testable, and lets each arm populate token/cost from whichever
    source it has (the pipeline's structured ``usage`` dict, or ReAct's
    ``profile.ndjson``).
    """

    session_id: str
    entity_count: int
    file_count: int
    base_passed: bool
    isa_passed: bool
    tox_passed: bool
    required_issue_count: int
    entity_counts: dict[str, int] = field(default_factory=dict)
    mit_score: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None


def _read_token_totals(session_id: str) -> tuple[int, int, str]:
    """Best-effort cumulative ``(input, output, last_model)`` from the profile.

    Reads ``profile.ndjson`` for the session and sums ``model`` ``node_end``
    token counts. Returns zeros on any failure — token display is advisory.
    """
    try:
        from builder.tools.dashboard import read_profile
        from builder.tools.profiler import SESSION_DIR

        profile_path = SESSION_DIR / session_id / "profile.ndjson"
        if not profile_path.exists():
            return 0, 0, ""
        records = read_profile(profile_path)
        model_ends = [
            r for r in records if r.get("event") == "node_end" and r.get("node") == "model"
        ]
        total_in = sum(int(r.get("input_tokens", 0) or 0) for r in model_ends)
        total_out = sum(int(r.get("output_tokens", 0) or 0) for r in model_ends)
        last_model = (model_ends[-1].get("model_name") or "") if model_ends else ""
        return total_in, total_out, last_model
    except Exception:
        logger.debug("token totals unavailable for %s", session_id, exc_info=True)
        return 0, 0, ""


def snapshot_from_engine(engine: AgentEngine) -> UiSnapshot:
    """Build a :class:`UiSnapshot` from a live engine (the one impure adapter).

    Reads entity/file counts and validation flags from ``engine.state`` and,
    best-effort, cumulative token/cost totals from the session profile.
    """
    state = engine.state
    entities = state.list_entities()
    counts: dict[str, int] = {}
    for e in entities:
        typ = getattr(e, "type", "Unknown")
        counts[typ] = counts.get(typ, 0) + 1

    val = state.validation
    mit = getattr(state, "mit_assessment", None)
    mit_score = getattr(mit, "overall_score", None) if mit is not None else None

    tokens_in, tokens_out, last_model = _read_token_totals(state.session_id)
    cost_usd: float | None = None
    if tokens_in + tokens_out > 0:
        try:
            from builder.config import get_model_provider
            from builder.pricing import compute_cost

            info = compute_cost(
                tokens_in, tokens_out, last_model, provider=get_model_provider()
            )
            cost_usd = info.get("total_cost")
        except Exception:
            logger.debug("cost unavailable for %s", state.session_id, exc_info=True)

    return UiSnapshot(
        session_id=state.session_id,
        entity_count=len(entities),
        file_count=len(state.scanned_files),
        base_passed=val.base_passed,
        isa_passed=val.isa_passed,
        tox_passed=val.tox_passed,
        required_issue_count=len(val.required_issues),
        entity_counts=counts,
        mit_score=mit_score,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
    )


# ---------------------------------------------------------------------------
# Pure renderers
# ---------------------------------------------------------------------------


def _dot(ok: bool) -> str:
    return _PASS_DOT if ok else _PENDING_DOT


def render_status_bar(snap: UiSnapshot) -> RenderableType:
    """A compact one-line status header (session · counts · validation · tokens).

    Rendered as a re-printed dim line (not a pinned bar, which would conflict
    with ``console.input``/``console.status``); includes one blank line above
    for breathing room.
    """
    token_str = ""
    if snap.tokens_in + snap.tokens_out > 0:
        from builder.pricing import format_cost

        cost_str = f"@{format_cost(snap.cost_usd)}" if snap.cost_usd is not None else ""
        total = snap.tokens_in + snap.tokens_out
        token_str = (
            f"  {_SEP}  [dim]tok {snap.tokens_in}→{snap.tokens_out} ({total})"
            f"{cost_str}[/dim]"
        )

    status = (
        f"[dim]{snap.session_id}[/dim]  {_SEP}  "
        f"[dim]{snap.entity_count} entities[/dim]  {_SEP}  "
        f"[dim]{snap.file_count} files[/dim]  {_SEP}  "
        f"{_dot(snap.base_passed)} [dim]base[/dim]  "
        f"{_dot(snap.isa_passed)} [dim]ISA[/dim]  "
        f"{_dot(snap.tox_passed)} [dim]Tox[/dim]"
        f"{token_str}"
    )
    return Group("", Padding(status, (0, 0, 0, 1)))


def render_reply(content: Any) -> RenderableType:
    """Render an agent reply: a green ``●`` marker beside left-indented Markdown.

    Accepts raw message ``content`` (str or content-block list) and flattens
    it first, so structured replies never leak (#341). Lighter than a
    full-width panel; continuation lines align under the marker. Includes the
    surrounding blank lines so both arms space replies identically.
    """
    text = flatten_message_content(content)
    try:
        body: RenderableType = Markdown(text)
    except Exception:
        body = text

    grid = Table.grid(padding=(0, 0))
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row("[green]●[/green]", body)
    return Group("", "", grid, "")


def render_resume_summary(snap: UiSnapshot) -> RenderableType:
    """The "Resumed Session" panel — session id, counts, MIT, validation, breakdown."""
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold", width=16)
    summary.add_column(style="white")

    summary.add_row("Session:", f"[cyan]{snap.session_id}[/cyan]")
    summary.add_row("Entities:", f"[green]{snap.entity_count}[/green]")
    summary.add_row("Files:", f"[green]{snap.file_count}[/green]")

    if snap.mit_score is not None:
        summary.add_row("MIT score:", f"[yellow]{snap.mit_score:.0%}[/yellow]")

    val_status = [
        "[green]base[/green]" if snap.base_passed else "[red]base[/red]",
        "[green]ISA[/green]" if snap.isa_passed else "[red]ISA[/red]",
        "[green]ISA-Tox[/green]" if snap.tox_passed else "[red]ISA-Tox[/red]",
    ]
    summary.add_row("Validation:", "  ".join(val_status))

    if snap.required_issue_count:
        summary.add_row("Issues:", f"[red]{snap.required_issue_count} REQUIRED[/red]")

    if snap.entity_counts:
        parts = ", ".join(
            f"[cyan]{k}[/cyan]={v}" for k, v in sorted(snap.entity_counts.items())
        )
        summary.add_row("Breakdown:", parts)

    return Panel(summary, title="[yellow]Resumed Session[/yellow]", border_style="yellow")


def render_goodbye(
    session_id: str, entity_counts: dict[str, int], *, resumable: bool
) -> RenderableType:
    """The goodbye panel — session id, entity breakdown, and a resume hint.

    Args:
        session_id: The session identifier to echo.
        entity_counts: Per-type entity counts (``{}`` renders "0").
        resumable: When true, include the ``--resume`` command line (the
            caller decides this, e.g. only when a ``sessions/`` dir exists).
    """
    t = Table.grid(padding=(0, 1))
    t.add_column(style="yellow bold", width=14)
    t.add_column(style="white")
    t.add_row("Session:", f"[cyan]{session_id}[/cyan]")

    if entity_counts:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(entity_counts.items()))
        t.add_row("Entities:", parts)
    else:
        t.add_row("Entities:", "0")

    if resumable:
        t.add_row(
            "Resume:",
            f"python -m main [cyan]--resume {session_id}[/cyan] [dim]--interactive[/dim]",
        )

    return Panel(t, title="[yellow]Goodbye![/yellow]", border_style="yellow")


# ---------------------------------------------------------------------------
# Boxed prompt
# ---------------------------------------------------------------------------


def boxed_input(console: Console, label: str = "❯") -> str:
    """Read one line of input inside a rounded box (Claude Code style).

    Renders an ephemeral rounded box via ``prompt_toolkit``; once submitted the
    box is erased and the line is echoed into the transcript so it persists.
    Falls back to ``console.input`` when stdin is not a TTY or ``prompt_toolkit``
    is unavailable. Raises ``KeyboardInterrupt`` (Ctrl+C) and ``EOFError``
    (Ctrl+D on an empty line), matching ``input()``.
    """

    def _fallback() -> str:
        return console.input(f"[bold cyan]{label} [/bold cyan]").strip()

    if not sys.stdin.isatty():
        return _fallback()
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
        from prompt_toolkit.layout.controls import (
            BufferControl,
            FormattedTextControl,
        )
        from prompt_toolkit.styles import Style
    except Exception:
        return _fallback()

    buf = Buffer(multiline=False)
    outcome: dict[str, Any] = {"exc": None}
    kb = KeyBindings()

    @kb.add("enter")
    def _(event: Any) -> None:
        event.app.exit(result=buf.text)

    @kb.add("c-c")
    def _(event: Any) -> None:
        outcome["exc"] = KeyboardInterrupt
        event.app.exit(result="")

    @kb.add("c-d")
    def _(event: Any) -> None:
        if not buf.text:
            outcome["exc"] = EOFError
            event.app.exit(result="")

    def _hline(left: str, right: str):
        def _get() -> list[tuple[str, str]]:
            w = get_app().output.get_size().columns
            return [("class:box", left + "─" * max(0, w - 2) + right)]

        return _get

    buf_window = Window(BufferControl(buffer=buf))
    middle = VSplit(
        [
            Window(
                FormattedTextControl([("class:box", "│ "), ("class:prompt", f"{label} ")]),
                width=4,
            ),
            buf_window,
            Window(FormattedTextControl([("class:box", " │")]), width=2),
        ],
        height=1,
    )
    root = HSplit(
        [
            Window(FormattedTextControl(_hline("╭", "╮")), height=1),
            middle,
            Window(FormattedTextControl(_hline("╰", "╯")), height=1),
        ]
    )
    style = Style.from_dict({"box": "fg:#5f5f5f", "prompt": "bold ansicyan"})
    app: Any = Application(
        layout=Layout(root, focused_element=buf_window),
        key_bindings=kb,
        style=style,
        full_screen=False,
    )
    try:
        text = app.run()
    except Exception:
        return _fallback()

    if outcome["exc"] is not None:
        raise outcome["exc"]
    text = (text or "").strip()
    if text:
        # The box was ephemeral — echo the submitted line into the transcript.
        console.print(f"[bold cyan]{label}[/bold cyan] {text}")
    return text
