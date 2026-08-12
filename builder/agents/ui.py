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

import atexit
import io
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, cast

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from builder.engine import AgentEngine

logger = logging.getLogger(__name__)

# rich's ``Console.color_system`` property widens to ``str | None`` on the way
# out, but ``Console.__init__`` only accepts this literal set on the way back in
# — so round-tripping one console's colour system into another needs a cast.
ColorSystem = Literal["auto", "standard", "256", "truecolor", "windows"]

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
# Log notices — quiet, deduplicated, styled like the rest of the transcript
# ---------------------------------------------------------------------------


class NoticeHandler(logging.Handler):
    """Render log records as dim one-line notices, each shown at most once.

    The default stderr handler prints
    ``2026-08-07 15:05:59 [WARNING] builder.tools._crate_mapping: …`` in plain
    white, at full width, every time the record fires. Two things make that
    hostile in an interactive session: the timestamp and dotted logger path are
    noise a user cannot act on, and the same record repeats on every build — one
    observed session printed the same four "not a term in the context" warnings
    forty-four times, tearing through the reply text and the input box.

    So: strip the machinery, dim the line so it reads as chrome rather than as
    the assistant talking, and collapse repeats. A repeat is *counted*, not
    shown; :attr:`suppressed` reports the total for anyone who wants to say so.
    The full, timestamped records are still available with ``-v``, which turns
    this handler off in favour of the standard stream handler.
    """

    _STYLE = {
        logging.CRITICAL: "bold red",
        logging.ERROR: "red",
        logging.WARNING: "dim yellow",
    }
    _DEFAULT_STYLE = "dim"

    def __init__(self, console: Console, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self._console = console
        self._seen: dict[str, int] = {}
        self._lock_seen = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            key = f"{record.name}\x00{message}"
            with self._lock_seen:
                seen = self._seen.get(key, 0)
                self._seen[key] = seen + 1
            if seen:
                return  # said once already; saying it again helps nobody
            style = self._STYLE.get(record.levelno, self._DEFAULT_STYLE)
            self._console.print(Text(f"· {message}", style=style), soft_wrap=False)
        except Exception:  # noqa: BLE001 — logging must never break the session
            self.handleError(record)

    @property
    def suppressed(self) -> int:
        """How many repeat records were counted but not printed."""
        with self._lock_seen:
            return sum(count - 1 for count in self._seen.values() if count > 1)


def install_notice_handler(
    console: Console | None = None, *, level: int = logging.WARNING
) -> NoticeHandler:
    """Route root logging through a :class:`NoticeHandler`, replacing stream output.

    Existing ``StreamHandler``s on the root logger are removed: leaving them
    attached would print every record twice, once raw and once quiet.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, NoticeHandler):
            root.removeHandler(handler)
    handler = NoticeHandler(console or get_console(), level=level)
    root.addHandler(handler)
    return handler


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
    unit-testable. Token/cost comes from ONE source in both arms:
    :func:`snapshot_from_engine` reads the session's ``profile.ndjson`` and
    nothing else. (It never reads the pipeline's returned ``usage`` dict — an
    earlier version of this docstring claimed it did, which is what made it easy
    to believe the pipeline's guidance tail was accounted when its calls simply
    never reached the profile, #384.)
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
    mit_assessed: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    # The most recent model call on its own. The prompt is rebuilt every call,
    # so this input figure IS the current context size — the number that shows
    # context bloat, which the session total (a sum over every re-send) hides.
    turn_tokens_in: int = 0
    turn_tokens_out: int = 0
    # Seconds spent inside model calls — the run's machine effort, as opposed to
    # the wall clock, which also counts the user reading and thinking.
    model_seconds: float = 0.0
    # The model actually answering. Read from the session profile once a reply
    # has come back, else the configured model — so a fresh session still names
    # what it is about to run.
    model: str = ""
    # Where the crate was written, and what is still open on it. Both belong in
    # the goodbye summary: "where did my crate go" and "is it finished" are the
    # two questions a user has when the session ends.
    crate_path: str = ""
    should_issue_count: int = 0
    may_issue_count: int = 0
    assessed_tiers: tuple[str, ...] = ()


@dataclass
class _TokenTail:
    """Where the incremental profile reader stopped, and the totals so far."""

    inode: int
    offset: int
    tokens_in: int
    tokens_out: int
    last_model: str
    # The last model call's own counts, kept alongside the running totals so the
    # per-turn figures cost no extra parsing.
    last_call_in: int = 0
    last_call_out: int = 0


# Per-profile-file resume points for :func:`_read_token_totals`, keyed by the
# resolved path (so a monkeypatched ``SESSION_DIR`` gets its own entry).
_TOKEN_TAILS: dict[str, _TokenTail] = {}


def _read_token_totals(session_id: str) -> tuple[int, int, str]:
    """Best-effort cumulative ``(input, output, last_model)`` from the profile.

    Sums the ``input_tokens`` / ``output_tokens`` of ``model`` ``node_end``
    events in the session's ``profile.ndjson``. Returns zeros on any failure —
    token display is advisory.

    The read is **incremental**: only bytes appended since the previous call are
    parsed, and only up to the last complete line (a half-written record is
    re-read next time). The pinned footer repaints about once a second over a
    file that grows to hundreds of KB in a long session, so re-parsing the whole
    profile per repaint would burn real CPU for an unchanged prefix. The cache
    resets itself whenever the file is replaced (inode change) or truncated.
    """
    try:
        from builder.tools.profiler import SESSION_DIR

        profile_path = SESSION_DIR / session_id / "profile.ndjson"
        if not profile_path.exists():
            return 0, 0, ""

        stat = profile_path.stat()
        key = str(profile_path)
        tail = _TOKEN_TAILS.get(key)
        if tail is None or tail.inode != stat.st_ino or stat.st_size < tail.offset:
            tail = _TokenTail(inode=stat.st_ino, offset=0, tokens_in=0, tokens_out=0, last_model="")
            _TOKEN_TAILS[key] = tail

        if stat.st_size > tail.offset:
            with profile_path.open("rb") as handle:
                handle.seek(tail.offset)
                chunk = handle.read(stat.st_size - tail.offset)
            complete = chunk.rfind(b"\n")
            if complete >= 0:
                tail.offset += complete + 1
                for raw in chunk[: complete + 1].splitlines():
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue  # a torn/foreign line is skipped, never fatal
                    if (
                        not isinstance(record, dict)
                        or record.get("event") != "node_end"
                        or record.get("node") != "model"
                    ):
                        continue
                    tail.last_call_in = int(record.get("input_tokens", 0) or 0)
                    tail.last_call_out = int(record.get("output_tokens", 0) or 0)
                    tail.tokens_in += tail.last_call_in
                    tail.tokens_out += tail.last_call_out
                    tail.last_model = record.get("model_name") or tail.last_model

        return tail.tokens_in, tail.tokens_out, tail.last_model
    except Exception:
        logger.debug("token totals unavailable for %s", session_id, exc_info=True)
        return 0, 0, ""


def _read_turn_tokens(session_id: str) -> tuple[int, int]:
    """The last model call's ``(input, output)`` tokens, or ``(0, 0)``.

    Reads the cache :func:`_read_token_totals` maintains, so it must be called
    after it (as :func:`snapshot_from_engine` does) and costs no extra parsing.
    """
    try:
        from builder.tools.profiler import SESSION_DIR

        tail = _TOKEN_TAILS.get(str(SESSION_DIR / session_id / "profile.ndjson"))
        return (tail.last_call_in, tail.last_call_out) if tail else (0, 0)
    except Exception:
        logger.debug("turn tokens unavailable for %s", session_id, exc_info=True)
        return 0, 0


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
    mit_assessed = bool(getattr(mit, "module_scores", None))

    generator = getattr(state, "generator", None)
    tokens_in, tokens_out, last_model = _read_token_totals(state.session_id)
    turn_in, turn_out = _read_turn_tokens(state.session_id)
    model_name = last_model
    if not model_name:
        try:
            from builder.config import get_active_model

            model_name = get_active_model() or ""
        except Exception:  # noqa: BLE001 — naming the model is advisory
            logger.debug("active model unavailable", exc_info=True)
    cost_usd: float | None = None
    if tokens_in + tokens_out > 0:
        try:
            from builder.config import get_model_provider
            from builder.pricing import compute_cost

            info = compute_cost(tokens_in, tokens_out, last_model, provider=get_model_provider())
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
        mit_assessed=mit_assessed,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        turn_tokens_in=turn_in,
        turn_tokens_out=turn_out,
        model=model_name,
        model_seconds=float(getattr(generator, "model_seconds", 0.0) or 0.0),
        crate_path=getattr(state.metadata, "output_path", "") or "",
        should_issue_count=len(val.should_issues),
        may_issue_count=len(val.may_issues),
        assessed_tiers=tuple(sorted(getattr(val, "assessed_tiers", ()) or ())),
    )


# ---------------------------------------------------------------------------
# Pure renderers
# ---------------------------------------------------------------------------


def _dot(ok: bool) -> str:
    return _PASS_DOT if ok else _PENDING_DOT


def _compact_model(name: str) -> str:
    """A model name short enough for the pinned line, still recognisable.

    Drops a provider prefix and a trailing release date — ``claude-sonnet-4``
    identifies the model as well as ``anthropic/claude-sonnet-4-20250514`` and
    leaves room for the counts that share the row. The header panel shows the
    full name, so nothing is lost.
    """
    short = (name or "").rsplit("/", 1)[-1]
    parts = short.split("-")
    if len(parts) > 1 and parts[-1].isdigit() and len(parts[-1]) == 8:
        short = "-".join(parts[:-1])
    return short


def _compact_seconds(seconds: float) -> str:
    """Model time as ``42s`` / ``7m12s`` / ``1h04m`` — short enough for the row."""
    total = int(round(seconds or 0))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def _compact_tokens(count: int) -> str:
    """Token count in a fixed-width-ish compact form: ``842``, ``12.9k``, ``2.5M``.

    A pinned line has one row to spend, and six-digit counts push the validation
    dots off a narrow terminal. Counts under 1000 are shown exactly — rounding
    the small numbers would lose the detail that matters at the start of a run.
    """
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.1f}M".replace(".0M", "M")


def _work_field(snap: UiSnapshot) -> tuple[str, str]:
    """The middle status slot: the scan at first, then what is left to fix.

    Returns ``(field_key, text)``.

    The file count is a startup fact — it is settled by the time the first entity
    exists and then never moves again, so a run spends its whole life looking at
    "54 files" telling it nothing. Once drafting begins the slot earns its place
    by showing the open findings instead.

    A tier that has not been swept shows as **locked** rather than as ``0``. This
    is honest, not decorative: ``build_and_validate`` defaults to the REQUIRED
    gate, and a gate is a floor — RECOMMENDED and OPTIONAL checks are not
    evaluated at all until the gate is lowered, so their counts are genuinely
    unknown, not zero. ``assessed_tiers`` is what the validator actually
    assessed, so it is the thing to read.
    """
    if snap.entity_count == 0:
        return "files", f"{snap.file_count} files"

    assessed = set(snap.assessed_tiers)
    parts: list[str] = []
    locked: list[str] = []
    for tier, label, count in (
        ("required", "req", snap.required_issue_count),
        ("recommended", "rec", snap.should_issue_count),
        ("optional", "opt", snap.may_issue_count),
    ):
        if tier in assessed:
            parts.append(f"{count} {label}")
        else:
            locked.append(label)
    if locked:
        parts.append(f"{'/'.join(locked)} locked")
    return "issues", " ".join(parts) if parts else "not validated"


def render_status_markup(snap: UiSnapshot, *, highlight: dict[str, str] | None = None) -> str:
    """The status line as one line of Rich markup (no padding, no newline).

    The single composition of the status fields, shared by the scrolling
    :func:`render_status_bar` and the :class:`PinnedFooter`, so the pinned line
    and the printed line can never drift apart.

    Args:
        snap: The state to render.
        highlight: Optional ``field key -> Rich style`` overrides, replacing that
            field's normal dim styling. :class:`StatusFader` uses this to tint
            the fields that just changed. Keys are the ones in
            :func:`status_field_values`.
    """
    styles = highlight or {}

    def field(key: str, text: str, style: str = "dim") -> str:
        return f"[{styles.get(key, style)}]{text}[/]"

    token_str = ""
    if snap.tokens_in + snap.tokens_out > 0:
        # Two decimals here, not `format_cost`'s six-then-four. This line is read
        # at a glance while the run moves; $0.004821 is precision nobody acts on,
        # and it changes width as it grows, which makes the whole segment jitter.
        # `format_cost` keeps its resolution for the places that settle up — the
        # goodbye summary and the dashboard.
        cost_str = f" @${snap.cost_usd:.2f}" if snap.cost_usd is not None else ""
        total = snap.tokens_in + snap.tokens_out
        # ↑/↓ are THIS turn — the prompt is rebuilt every call, so ↑ is the live
        # context size and its drift is the bloat signal. Before the first call
        # completes there is no turn figure, so fall back to the session numbers
        # rather than showing a misleading pair of zeros.
        turn_in = snap.turn_tokens_in or snap.tokens_in
        turn_out = snap.turn_tokens_out or snap.tokens_out
        # The model sits at the head of the spend segment: it is what the tokens
        # and the cost are being spent ON, so the three read as one fact.
        model_prefix = (
            f"{field('model', _compact_model(snap.model))}  {_SEP}  " if snap.model else ""
        )
        # Model time, not wall clock: the clock beside it counts the user
        # thinking, which is not what the run cost.
        time_str = f"  {_compact_seconds(snap.model_seconds)}" if snap.model_seconds else ""
        token_str = (
            "  "
            + _SEP
            + "  "
            + model_prefix
            + field(
                "tokens",
                f"↑{_compact_tokens(turn_in)} ↓{_compact_tokens(turn_out)}"
                f"  {_compact_tokens(total)} tok{cost_str}{time_str}",
            )
        )

    return (
        f"{field('session', snap.session_id)}  {_SEP}  "
        f"{field('entities', f'{snap.entity_count} entities')}  {_SEP}  "
        f"{field(*_work_field(snap))}  {_SEP}  "
        f"{_dot(snap.base_passed)} {field('base', 'base')}  "
        f"{_dot(snap.isa_passed)} {field('isa', 'ISA')}  "
        f"{_dot(snap.tox_passed)} {field('tox', 'Tox')}"
        f"{token_str}"
    )


def status_field_values(snap: UiSnapshot) -> dict[str, Any]:
    """The comparable value behind each status field, keyed as in *highlight*."""
    return {
        "session": snap.session_id,
        "model": snap.model,
        "entities": snap.entity_count,
        "files": snap.file_count,
        # The middle slot swaps from the scan to the open findings once drafting
        # starts (`_work_field`); both keys are listed so the fader tints
        # whichever one is currently on screen.
        "issues": (
            snap.required_issue_count,
            snap.should_issue_count,
            snap.may_issue_count,
            snap.assessed_tiers,
        ),
        "base": snap.base_passed,
        "isa": snap.isa_passed,
        "tox": snap.tox_passed,
        # The per-turn figures are included so the fader tints the segment when
        # a new turn lands, not only when the cumulative cost ticks over.
        "tokens": (
            snap.tokens_in,
            snap.tokens_out,
            snap.cost_usd,
            snap.turn_tokens_in,
            snap.turn_tokens_out,
        ),
    }


# How a just-changed field cools off: (age in seconds below which it applies,
# style). Bright at the moment of change, deepening, then back to normal dim —
# enough to catch the eye mid-run without the line ever looking like an error.
_FADE_STEPS: tuple[tuple[float, str], ...] = (
    (0.7, "bold #9cc3ff"),
    (1.6, "#5f87ff"),
    (2.6, "#4a5f9e"),
    (3.5, "#3b4a72"),
)

# The same ramp in red, for a change that made things WORSE — entities or files
# disappearing, or a validation layer that used to pass and no longer does. Blue
# for both would report "something moved" while hiding which way, and losing a
# passing profile is the one event in this line worth interrupting someone for.
_FADE_STEPS_WORSE: tuple[tuple[float, str], ...] = (
    (0.7, "bold #ff9c9c"),
    (1.6, "#ff5f5f"),
    (2.6, "#b34a4a"),
    (3.5, "#7d3838"),
)


def _fade_style(age: float, *, worse: bool = False) -> str | None:
    """The highlight style for a change *age* seconds old (``None`` once cold)."""
    for limit, style in _FADE_STEPS_WORSE if worse else _FADE_STEPS:
        if age < limit:
            return style
    return None


def _is_worse(key: str, previous: Any, current: Any) -> bool:
    """Whether *key* moving from *previous* to *current* is a regression.

    Counts fewer entities or files, and a validation profile that stops
    passing. Token/cost growth is deliberately NOT a regression: it only ever
    goes up, so painting it red would make the normal case look alarming.
    """
    if key in ("entities", "files"):
        try:
            return int(current) < int(previous)
        except (TypeError, ValueError):
            return False
    if key in ("base", "isa", "tox"):
        return bool(previous) and not bool(current)
    return False


class StatusFader:
    """Tracks which status fields changed and fades their highlight over time.

    A pinned line is easy to stop reading: the numbers move, but nothing draws
    the eye to WHICH number moved. This tints each field for a few seconds after
    it changes — entities climbing, a validation dot flipping, cost ticking up —
    and lets the tint decay back to normal, so the footer reports change without
    becoming a permanent light show. Blue for ordinary progress, red when the
    change is a regression (see :func:`_is_worse`), so direction is visible at a
    glance rather than needing the number to be read.

    Pure apart from the clock, which is injectable for tests.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or monotonic
        self._values: dict[str, Any] = {}
        # key -> (when it changed, whether that change was a regression)
        self._changed_at: dict[str, tuple[float, bool]] = {}

    def markup(self, snap: UiSnapshot) -> str:
        """Render *snap*, highlighting fields that changed since the last call."""
        now = self._clock()
        for key, value in status_field_values(snap).items():
            if key in self._values and self._values[key] != value:
                self._changed_at[key] = (now, _is_worse(key, self._values[key], value))
            self._values[key] = value

        highlight: dict[str, str] = {}
        for key, (changed_at, worse) in list(self._changed_at.items()):
            style = _fade_style(now - changed_at, worse=worse)
            if style is None:
                del self._changed_at[key]  # cold: stop tracking it
            else:
                highlight[key] = style
        return render_status_markup(snap, highlight=highlight)


def render_status_bar(snap: UiSnapshot) -> RenderableType:
    """A compact one-line status header (session · counts · validation · tokens).

    The scrolling form: a re-printed dim line with one blank line above for
    breathing room. Used when the terminal cannot host the pinned footer
    (non-TTY, dumb terminal, or the footer explicitly disabled).
    """
    return Group("", Padding(render_status_markup(snap), (0, 0, 0, 1)))


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


def render_resume_summary(snap: UiSnapshot, *, resumed: bool) -> RenderableType:
    """The session summary panel — session id, counts, MIT, validation, breakdown.

    *resumed* is the panel's provenance and is **mandatory**: it decides only the
    title ("Resumed Session" vs "Session"), never the body. It cannot be derived
    from *snap* — a fresh ``--input`` run has already scanned files (and may have
    drafted entities) by the time this renders, so a populated snapshot is not
    evidence of a resume (#410). The caller knows; the renderer must be told.
    """
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold", width=16)
    summary.add_column(style="white")

    summary.add_row("Session:", f"[cyan]{snap.session_id}[/cyan]")
    if snap.model:
        summary.add_row("Model:", f"[cyan]{snap.model}[/cyan]")
    summary.add_row("Entities:", f"[green]{snap.entity_count}[/green]")
    summary.add_row("Files:", f"[green]{snap.file_count}[/green]")

    if snap.mit_assessed:
        from builder.tools.dashboard import format_mit_coverage

        mit_text, mit_color = format_mit_coverage(snap.mit_score, assessed=True)
        summary.add_row("MIT score:", f"[{mit_color}]{mit_text}[/{mit_color}]")

    val_status = [
        "[green]base[/green]" if snap.base_passed else "[red]base[/red]",
        "[green]ISA[/green]" if snap.isa_passed else "[red]ISA[/red]",
        "[green]ISA-Tox[/green]" if snap.tox_passed else "[red]ISA-Tox[/red]",
    ]
    summary.add_row("Validation:", "  ".join(val_status))

    if snap.required_issue_count:
        summary.add_row("Issues:", f"[red]{snap.required_issue_count} REQUIRED[/red]")

    if snap.entity_counts:
        parts = ", ".join(f"[cyan]{k}[/cyan]={v}" for k, v in sorted(snap.entity_counts.items()))
        summary.add_row("Breakdown:", parts)

    title = "Resumed Session" if resumed else "Session"
    return Panel(summary, title=f"[yellow]{title}[/yellow]", border_style="yellow")


def render_goodbye(
    session_id: str,
    entity_counts: dict[str, int],
    *,
    resumable: bool,
    snap: UiSnapshot | None = None,
) -> RenderableType:
    """The goodbye panel — what was built, where it went, and what it cost.

    A session ends with three unanswered questions: where is my crate, is it
    finished, and what did that cost me? The entity breakdown alone answers
    none of them. When *snap* is supplied, the panel adds the export path, the
    outstanding issues per assessed tier, and the run's token spend.

    Args:
        session_id: The session identifier to echo.
        entity_counts: Per-type entity counts (``{}`` renders "0").
        resumable: When true, include the ``--resume`` command line (the
            caller decides this, e.g. only when a ``sessions/`` dir exists).
        snap: Optional session snapshot supplying crate path, issues and cost.
            Omitted (the default) renders the original three-row panel.
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

    if snap is not None:
        t.add_row(
            "Crate:",
            f"[cyan]{snap.crate_path}[/cyan]"
            if snap.crate_path
            else "[yellow]not exported[/yellow] [dim]— ask me to export next time[/dim]",
        )

        # Only report a tier that actually ran: "0 recommended issues" would
        # otherwise claim a clean bill of health for checks nobody performed.
        conformance = "  ".join(
            f"[{'green' if ok else 'red'}]{label}[/{'green' if ok else 'red'}]"
            for label, ok in (
                ("base", snap.base_passed),
                ("ISA", snap.isa_passed),
                ("Tox", snap.tox_passed),
            )
        )
        open_counts: list[str] = [
            f"[red]{snap.required_issue_count} required[/red]"
            if snap.required_issue_count
            else "[green]0 required[/green]"
        ]
        if "recommended" in snap.assessed_tiers:
            open_counts.append(f"{snap.should_issue_count} recommended")
        if "optional" in snap.assessed_tiers:
            open_counts.append(f"{snap.may_issue_count} optional")
        t.add_row("Validation:", f"{conformance}  [dim]·[/dim]  " + ", ".join(open_counts))

        if snap.tokens_in + snap.tokens_out:
            from builder.pricing import format_cost

            total = _compact_tokens(snap.tokens_in + snap.tokens_out)
            cost = format_cost(snap.cost_usd) if snap.cost_usd is not None else "cost unknown"
            model = f" [dim]on {_compact_model(snap.model)}[/dim]" if snap.model else ""
            # Model time, not wall clock — the elapsed clock includes every pause
            # the user took and says nothing about what the run cost.
            spent = (
                f" [dim]·[/dim] [cyan]{_compact_seconds(snap.model_seconds)}[/cyan] "
                "[dim]model time[/dim]"
                if snap.model_seconds
                else ""
            )
            t.add_row("This run:", f"[cyan]{cost}[/cyan] [dim]({total} tokens)[/dim]{model}{spent}")

    if resumable:
        t.add_row(
            "Resume:",
            f"python -m main [cyan]--resume {session_id}[/cyan] [dim]--interactive[/dim]",
        )

    return Panel(t, title="[yellow]Goodbye![/yellow]", border_style="yellow")


# ---------------------------------------------------------------------------
# Pinned footer — a status line held on the bottom rows of the terminal
# ---------------------------------------------------------------------------

# Set to 1/true/yes to force the scrolling status bar instead of the pinned
# footer. The escape sequences below are standard (DECSTBM), but a terminal that
# mishandles them corrupts the whole session, so there must be an off switch
# that needs no code change.
FOOTER_DISABLE_ENV = "VITRO_NO_PINNED_FOOTER"

# Rows the footer reserves at the bottom: a rule, the activity line (the
# spinner, when work is running), and the status line.
_FOOTER_ROWS = 3

# Below this terminal height the footer is not worth three of the user's rows.
_FOOTER_MIN_HEIGHT = 10

# Consecutive write failures after which the footer disables itself for good
# rather than fighting a terminal that clearly does not want it.
_FOOTER_MAX_FAILURES = 3

# Key hint shown at the right-hand end of the footer's rule. Ctrl+C interrupts
# the agent mid-run and hands the prompt back (the session and the crate
# survive); Ctrl+D on an empty prompt ends the session. Both are otherwise
# undiscoverable while the agent is working.
_FOOTER_KEY_HINT = "ctrl+c interrupt · ctrl+d exit"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class PinnedFooter:
    """A status line pinned to the bottom rows of the terminal.

    Mechanism: a DEC scrolling region (``CSI 1;{h-2} r``) shrinks the terminal's
    scroll area to everything above the last two rows, so ordinary output —
    Rich prints, the spinner's ``Live`` region, the ``prompt_toolkit`` input box
    — scrolls underneath a footer that is repainted in place. Nothing else in
    the session has to know the footer exists.

    Every paint is a single ``write`` wrapped in save-cursor/restore-cursor, and
    is taken under the Rich console's own lock, so it cannot interleave with a
    Rich repaint and cannot move the cursor out from under one.

    The footer is **best-effort chrome**: it is inactive unless stdout is a real
    terminal, it disables itself after a few failed writes, and
    :meth:`stop` — which is also registered with :mod:`atexit` — always restores
    the full scrolling region. Callers never need to branch on any of that,
    except to fall back to the scrolling status bar when :attr:`active` is
    false.
    """

    def __init__(
        self,
        console: Console,
        provider: Callable[[], str],
        *,
        interval: float = 1.0,
    ) -> None:
        """Build a footer.

        Args:
            console: The Rich console that owns the terminal.
            provider: Called on every repaint; returns one line of Rich markup.
                Kept as a callback (not a value) so the footer always paints
                *current* state, whoever triggers the repaint.
            interval: Seconds between background repaints.
        """
        self._console = console
        self._provider = provider
        self._interval = interval
        self._file = getattr(console, "file", sys.stdout)
        # Rich serialises its own output on Console._lock; sharing it is what
        # keeps a footer paint from landing inside a Live repaint.
        self._lock: Any = getattr(console, "_lock", None) or threading.RLock()
        self._active = False
        self._failures = 0
        self._size: tuple[int, int] = (0, 0)
        self._activity: str | None = None
        self._paused = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # -- capability ----------------------------------------------------------

    @staticmethod
    def supported(console: Console) -> bool:
        """Whether *console* can host a pinned footer (fail-closed)."""
        if _truthy(os.environ.get(FOOTER_DISABLE_ENV)):
            return False
        if not getattr(console, "is_terminal", False):
            return False
        if getattr(console, "is_dumb_terminal", False):
            return False
        if (os.environ.get("TERM") or "").strip().lower() in {"", "dumb"}:
            return False
        stream = getattr(console, "file", None)
        try:
            return bool(stream is not None and stream.isatty())
        except Exception:
            return False

    @property
    def active(self) -> bool:
        """Whether the footer currently owns the bottom rows."""
        return self._active

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Reserve the bottom rows and begin repainting (no-op if unsupported)."""
        if self._active or not self.supported(self._console):
            return
        width, height = self._terminal_size()
        if height < _FOOTER_MIN_HEIGHT:
            return
        # Scroll the reserved rows into existence FIRST, then step back up into
        # what is about to become the region: without this the footer would be
        # painted over whatever occupied the bottom rows.
        if not self._write("\n" * _FOOTER_ROWS + f"\x1b[{_FOOTER_ROWS}A"):
            return
        self._active = True
        self._apply_region(width, height)
        atexit.register(self.stop)
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()
        self.refresh()

    def stop(self) -> None:
        """Restore the full scrolling region and clear the footer rows.

        Idempotent, and safe to call from ``atexit`` or an exception path — a
        session that ends without this leaves the user's terminal unable to
        scroll its bottom rows.
        """
        if not self._active:
            return
        self._active = False
        self._stopping.set()
        _, height = self._terminal_size()
        cleared = "".join(
            f"\x1b[{max(1, height - offset)};1H\x1b[2K" for offset in range(_FOOTER_ROWS)
        )
        self._write("\x1b7" + cleared + "\x1b[r" + "\x1b8")
        try:
            atexit.unregister(self.stop)
        except Exception:  # noqa: BLE001 — teardown must never raise
            logger.debug("footer: atexit.unregister failed", exc_info=True)

    def __enter__(self) -> PinnedFooter:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- painting ------------------------------------------------------------

    def refresh(self) -> None:
        """Repaint the footer now (safe to call from any thread; never raises)."""
        if not self._active or self._paused.is_set():
            return
        try:
            width, height = self._terminal_size()
            if height < _FOOTER_MIN_HEIGHT:
                # The window shrank below the point where reserving two rows is
                # reasonable — give them back rather than squatting on them.
                self.stop()
                return
            if (width, height) != self._size:
                self._apply_region(width, height)
            markup = self._provider()
        except Exception:  # noqa: BLE001 — chrome must never break a build
            logger.debug("footer: refresh failed", exc_info=True)
            return

        line = self._to_ansi(markup, width - 1)
        rule = self._to_ansi(self._rule_markup(width - 1), width - 1)
        activity = self._to_ansi(self._activity or "", width - 1)
        self._write(
            "\x1b7"
            + f"\x1b[{height - 2};1H\x1b[2K"
            + rule
            + f"\x1b[{height - 1};1H\x1b[2K"
            + activity
            + f"\x1b[{height};1H\x1b[2K"
            + line
            + "\x1b8"
        )

    def _rule_markup(self, width: int) -> str:
        """The separator rule, carrying the key hint at its right-hand end.

        The hint rides on the rule rather than taking a row of its own: the two
        keys that matter while the agent is working are only discoverable by
        guessing otherwise, and the rule is the one line here with space to
        spare. Dropped when the terminal is too narrow to hold both.
        """
        if width <= 0:
            return ""
        if not _FOOTER_KEY_HINT or width < len(_FOOTER_KEY_HINT) + 12:
            return f"[grey35]{'─' * width}[/grey35]"
        dashes = width - len(_FOOTER_KEY_HINT) - 3
        return (
            f"[grey35]{'─' * dashes}[/grey35] "
            f"[grey42]{_FOOTER_KEY_HINT}[/grey42] "
            f"[grey35]─[/grey35]"
        )

    def line_width(self) -> int:
        """Columns a footer row can paint into (0 when the footer is inactive).

        Handed to the spinner so a streamed reply tail can fill the row exactly
        — the footer truncates the HEAD of an over-long line, which for a
        moving tail would show the wrong end of the text.
        """
        if not self._active:
            return 0
        width, _height = self._terminal_size()
        return max(0, width - 1)

    def set_activity(self, markup: str | None) -> None:
        """Show (``None`` clears) the working line on the footer's middle row.

        The spinner pushes here instead of opening its own Rich ``Live`` region,
        so the "what is running right now" line holds still at the bottom
        instead of scrolling away with the transcript.
        """
        self._activity = markup or None
        self.refresh()

    def pause(self) -> None:
        """Stop repainting (the rows keep their last paint) — for a full-screen op."""
        self._paused.set()

    def resume(self) -> None:
        """Resume repainting after :meth:`pause` and paint immediately."""
        self._paused.clear()
        self.refresh()

    # -- internals -----------------------------------------------------------

    def _terminal_size(self) -> tuple[int, int]:
        """The output terminal's real ``(width, height)``, straight from the fd.

        Deliberately NOT ``console.size``: Rich prefers the ``COLUMNS``/``LINES``
        environment variables, which many shells set once and never update, so a
        resized window would leave the footer painting over live content or off
        the bottom of the screen. The ioctl is always current, which also makes
        resize handling fall out for free. Falls back to Rich when the stream has
        no usable descriptor (a captured stream in a test).
        """
        try:
            size = os.get_terminal_size(self._file.fileno())
            if size.columns > 0 and size.lines > 0:
                return size.columns, size.lines
        except Exception:  # noqa: BLE001 — not a real fd; fall through to Rich
            logger.debug("footer: terminal size unavailable", exc_info=True)
        width, height = self._console.size
        return width, height

    def _apply_region(self, width: int, height: int) -> None:
        """(Re-)set the scrolling region to everything above the footer rows.

        Setting DECSTBM homes the cursor, so the sequence is wrapped in
        save/restore — otherwise the next print would land at the top of the
        screen and overwrite the transcript.
        """
        bottom = max(1, height - _FOOTER_ROWS)
        if self._write("\x1b7" + f"\x1b[1;{bottom}r" + "\x1b8"):
            self._size = (width, height)

    def _to_ansi(self, markup: str, width: int) -> str:
        """Render one line of Rich markup to ANSI, truncated to *width* columns.

        Truncation is to ``width`` (already one short of the terminal) so the
        write can never reach the final column: on an auto-wrap terminal that
        would scroll the screen out from under the region.
        """
        if width <= 0:
            return ""
        try:
            text = Text.from_markup(markup)
            text.truncate(width, overflow="ellipsis")
            buffer = io.StringIO()
            scratch = Console(
                file=buffer,
                width=width,
                force_terminal=True,
                color_system=cast("ColorSystem | None", self._console.color_system),
                highlight=False,
                soft_wrap=True,
                legacy_windows=False,
            )
            scratch.print(text, end="")
            return buffer.getvalue()
        except Exception:  # noqa: BLE001 — fall back to unstyled text
            logger.debug("footer: markup render failed", exc_info=True)
            return Text.from_markup(markup).plain[:width]

    def _write(self, payload: str) -> bool:
        """Write one escape payload under the console lock; disable on repeat failure."""
        try:
            with self._lock:
                self._file.write(payload)
                self._file.flush()
            self._failures = 0
            return True
        except Exception:  # noqa: BLE001 — a terminal that rejects this loses the footer
            logger.debug("footer: write failed", exc_info=True)
            self._failures += 1
            if self._failures >= _FOOTER_MAX_FAILURES and self._active:
                logger.debug("footer: disabling after %d failed writes", self._failures)
                self._active = False
                self._stopping.set()
            return False

    def _tick(self) -> None:
        """Daemon loop: repaint so tokens/cost advance while the agent works."""
        while not self._stopping.wait(self._interval):
            if self._active and not self._paused.is_set():
                self.refresh()


class TransientReplies:
    """Prints agent replies so that running commentary overwrites itself.

    An autonomous run narrates every step — "Let me fix the two issues", "Let me
    build and validate" — and each line is superseded by the next one seconds
    later. Scrolled into the transcript they bury the output that matters; kept
    transient, the newest one simply replaces the last.

    A transient reply is erased only when it is still the last thing on screen.
    Anything else printing in between (an export path, a tool result, the user's
    own line) calls :meth:`invalidate`, and the reply then stays put rather than
    having its lines cut out from under whatever followed it.

    Off a terminal this is a plain passthrough, so piped output and CI logs keep
    the full transcript.
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._height = 0

    def invalidate(self) -> None:
        """Forget the last reply — something else has been printed since."""
        self._height = 0

    def print(self, content: Any, *, transient: bool = False) -> None:
        """Print *content* as an agent reply, optionally as overwritable narration."""
        if not content:
            return
        renderable = render_reply(content)
        erasable = bool(getattr(self._console, "is_terminal", False))
        if erasable and self._height:
            # Walk up over the previous reply, clearing each line. Deliberately
            # NOT "erase to end of screen": that would also wipe the pinned
            # footer on the bottom rows.
            try:
                self._console.file.write("\x1b[1A\x1b[2K" * self._height)
                self._console.file.flush()
            except Exception:  # noqa: BLE001 — chrome must never break a turn
                logger.debug("transient reply erase failed", exc_info=True)
        self._height = 0
        self._console.print(renderable)
        if erasable and transient:
            try:
                self._height = len(self._console.render_lines(renderable, pad=False))
            except Exception:  # noqa: BLE001 — fall back to leaving it on screen
                logger.debug("transient reply height failed", exc_info=True)


def make_status_footer(engine: AgentEngine, console: Console | None = None) -> PinnedFooter:
    """A :class:`PinnedFooter` showing *engine*'s live status line (both arms).

    The provider re-snapshots the engine on every repaint, so entity counts,
    validation dots, tokens and cost track the run without any call site having
    to push updates, and a :class:`StatusFader` tints whatever just changed.

    The repaint interval is well under the fade so the highlight decays smoothly
    rather than stepping; each repaint is a state read plus an incremental
    profile tail-read, which is why that is affordable.
    """
    target = console or get_console()
    fader = StatusFader()
    return PinnedFooter(
        target,
        lambda: fader.markup(snapshot_from_engine(engine)),
        interval=0.25,
    )


# ---------------------------------------------------------------------------
# Print-for-engine helpers — the ONE place "snapshot → render → print to the
# shared console" lives, so both build arms call the same code instead of each
# inlining its own copy (#344). The pure render_* above stay unit-testable; these
# thin impure wrappers are what the ReAct loop and the pipeline both invoke.
# ---------------------------------------------------------------------------


def print_status_bar(engine: AgentEngine, footer: PinnedFooter | None = None) -> None:
    """Print the one-line status bar for *engine*'s current state (both arms).

    The per-prompt status line the ReAct loop shows before every input and the
    pipeline shows before every guidance question. ``engine.state.validation`` is
    authoritative by the time either arm calls this (the pipeline's final
    ``build_and_validate`` runs via ``engine.run_tool``, whose #153 write-back
    folds conformance into state), so the base/ISA/Tox dots read real values.

    When an active *footer* is passed, the same information is already pinned to
    the bottom of the terminal, so this repaints it instead of printing a second
    copy into the transcript.
    """
    if footer is not None and footer.active:
        footer.refresh()
        return
    get_console().print(render_status_bar(snapshot_from_engine(engine)))


def print_resume_summary(engine: AgentEngine, *, resumed: bool) -> None:
    """Print the session summary panel when there is anything to show (both arms).

    On an empty session (no entities, no scanned files) there is no panel to draw,
    so it degrades to a one-line ``session <id> · model <model>`` header — the
    model about to spend the user's money is worth stating up front. With no model
    resolved either, it prints nothing at all, so callers still need no guard of
    their own.

    That emptiness check is about *content*; it is not a resume test. *resumed*
    carries provenance and is mandatory precisely so no call site can fall back to
    inferring it from content (#410) — the two are independent, and conflating
    them is what labelled every fresh ``--input`` run a resume.
    """
    snap = snapshot_from_engine(engine)
    console = get_console()
    if snap.entity_count or snap.file_count:
        console.print(render_resume_summary(snap, resumed=resumed))
        console.print()
    elif snap.model:
        # Nothing built yet, so the panel would be an empty box — but the model
        # about to spend the user's money is still worth stating up front.
        console.print(
            f"[dim]session[/dim] [cyan]{snap.session_id}[/cyan]  {_SEP}  "
            f"[dim]model[/dim] [cyan]{snap.model}[/cyan]"
        )
        console.print()


def print_goodbye(engine: AgentEngine, *, resumable: bool | None = None) -> None:
    """Print the goodbye panel for *engine*'s session, with breathing room (both arms).

    Args:
        engine: The engine whose ``state`` (session id + entity breakdown) is shown.
        resumable: Whether to include the ``--resume`` hint. Defaults to *None* →
            shown when a ``sessions/`` directory exists (the check both arms used);
            pass an explicit bool to override.
    """
    if resumable is None:
        resumable = Path("sessions").is_dir()
    snap = snapshot_from_engine(engine)
    console = get_console()
    console.print()
    console.print(
        render_goodbye(snap.session_id, snap.entity_counts, resumable=resumable, snap=snap)
    )
    console.print()


# ---------------------------------------------------------------------------
# Boxed prompt
# ---------------------------------------------------------------------------


def boxed_input(
    console: Console,
    label: str = "❯",
    *,
    on_render: Callable[[], None] | None = None,
) -> str:
    """Read one line of input inside a rounded box (Claude Code style).

    Renders an ephemeral rounded box via ``prompt_toolkit``; once submitted the
    box is erased and the line is echoed into the transcript so it persists.
    Falls back to ``console.input`` when stdin is not a TTY or ``prompt_toolkit``
    is unavailable. Raises ``KeyboardInterrupt`` (Ctrl+C) and ``EOFError``
    (Ctrl+D on an empty line), matching ``input()``.

    Args:
        console: The Rich console to fall back to and echo through.
        label: The prompt glyph shown inside the box.
        on_render: Called after every ``prompt_toolkit`` repaint. The pinned
            footer passes its refresh here: prompt_toolkit erases from the
            cursor to the end of the screen when it redraws, which wipes the
            footer rows, so they are repainted on the same beat instead of
            waiting for the next background tick (which would read as flicker).
    """

    def _fallback() -> str:
        return console.input(f"[bold cyan]{label} [/bold cyan]").strip()

    if not sys.stdin.isatty():
        return _fallback()
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.output import create_output

        # Some terminal frontends (including certain IDE/WSL PTYs) do not answer
        # the cursor-position request prompt_toolkit uses during startup. In that
        # case prompt_toolkit can wait indefinitely, so use the safe line-input
        # fallback instead of attempting to start the full-screen application.
        output = create_output()
        if not getattr(output, "responds_to_cpr", True):
            return _fallback()
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

    def _after_render(_app: Any) -> None:
        if on_render is None:
            return
        try:
            on_render()
        except Exception:  # noqa: BLE001 — chrome must never break the prompt
            logger.debug("boxed_input: on_render failed", exc_info=True)

    app: Any = Application(
        layout=Layout(root, focused_element=buf_window),
        key_bindings=kb,
        style=style,
        full_screen=False,
        output=output,
        after_render=_after_render,
        # Without this the box is NOT ephemeral: prompt_toolkit leaves it on
        # screen, and the echo below then prints the same line a second time, so
        # every answered prompt appeared twice in the transcript.
        erase_when_done=True,
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


def select_option(
    console: Console,
    options: list[str],
    *,
    default: int = 0,
    hint: str | None = None,
    on_render: Callable[[], None] | None = None,
) -> int | None:
    """Choose one of *options* in a rounded box; return its index (``None`` = cancel).

    The choice starts on *default* and moves with ↑/↓ (or j/k); Enter takes the
    highlighted row and a number key jumps straight to it. This replaces the
    "print a numbered list, then ask ``Approve? [Y/n]``" prompt, which asked two
    different questions at once — a menu and a yes/no — and left the user unsure
    which one the answer applied to.

    Falls back to a plain numbered ``input()`` (Enter = the default) when stdin
    is not a TTY or ``prompt_toolkit`` is unavailable, so piped and CI runs keep
    working unchanged.

    Args:
        console: The Rich console to render the fallback and the echo through.
        options: The choices, in display order. Must be non-empty.
        default: Index highlighted first — the safe/expected answer. Callers that
            must fail closed (a filesystem-scope escalation) pass the denying
            option here so Enter cannot widen access by accident.
        hint: Optional one-line prompt shown above the choices.
        on_render: Called after every repaint (the pinned footer's refresh).

    Returns:
        The selected index, or ``None`` when the user cancelled (Ctrl+C / Esc)
        or gave no answer.
    """
    if not options:
        return None
    default = max(0, min(default, len(options) - 1))

    def _fallback() -> int | None:
        if hint:
            console.print(f"[bold]{hint}[/bold]")
        for index, option in enumerate(options, start=1):
            marker = "❯" if index - 1 == default else " "
            console.print(f" {marker} [cyan]{index}[/cyan]. {option}")
        try:
            raw = console.input(
                f"[bold cyan]Select[/bold cyan] [dim][1-{len(options)}, "
                f"Enter = {default + 1}][/dim] "
            )
        except EOFError:
            return None
        # Same answer grammar as the plain-terminal chooser (number, yes/no word,
        # or a prefix), so a piped run behaves like an interactive one.
        from builder.tools.hitl import match_choice

        return match_choice(raw, options, default)

    if not sys.stdin.isatty():
        return _fallback()
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.output import create_output

        output = create_output()
        if not getattr(output, "responds_to_cpr", True):
            return _fallback()
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
    except Exception:
        return _fallback()

    cursor = {"index": default}

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(event: Any) -> None:
        cursor["index"] = (cursor["index"] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _(event: Any) -> None:
        cursor["index"] = (cursor["index"] + 1) % len(options)

    @kb.add("enter")
    def _(event: Any) -> None:
        event.app.exit(result=cursor["index"])

    @kb.add("c-c")
    @kb.add("escape", eager=True)
    def _(event: Any) -> None:
        event.app.exit(result=None)

    for position in range(min(len(options), 9)):

        @kb.add(str(position + 1))
        def _(event: Any, position: int = position) -> None:
            cursor["index"] = position
            event.app.exit(result=position)

    def _hline(left: str, right: str):
        def _get() -> list[tuple[str, str]]:
            width = get_app().output.get_size().columns
            return [("class:box", left + "─" * max(0, width - 2) + right)]

        return _get

    def _rows() -> list[tuple[str, str]]:
        width = get_app().output.get_size().columns
        inner = max(0, width - 4)  # the two border columns on each side
        fragments: list[tuple[str, str]] = []
        for index, option in enumerate(options):
            selected = index == cursor["index"]
            marker = "❯ " if selected else "  "
            body = f"{marker}{index + 1}. {option}"[:inner].ljust(inner)
            fragments.append(("class:box", "│ "))
            fragments.append(("class:selected" if selected else "class:option", body))
            fragments.append(("class:box", " │"))
            fragments.append(("", "\n"))
        return fragments

    def _hint_row() -> list[tuple[str, str]]:
        width = get_app().output.get_size().columns
        inner = max(0, width - 4)
        return [
            ("class:box", "│ "),
            ("class:hint", str(hint)[:inner].ljust(inner)),
            ("class:box", " │"),
        ]

    body_rows = [Window(FormattedTextControl(_rows), height=len(options))]
    header = [Window(FormattedTextControl(_hint_row), height=1)] if hint else []
    root = HSplit(
        [
            Window(FormattedTextControl(_hline("╭", "╮")), height=1),
            *header,
            *body_rows,
            Window(FormattedTextControl(_hline("╰", "╯")), height=1),
            Window(
                FormattedTextControl(
                    [("class:help", "  ↑/↓ move · enter select · 1-9 jump · esc cancel")]
                ),
                height=1,
            ),
        ]
    )
    style = Style.from_dict(
        {
            "box": "fg:#5f5f5f",
            "hint": "bold",
            "option": "",
            "selected": "bold ansicyan",
            "help": "fg:#5f5f5f",
        }
    )

    def _after_render(_app: Any) -> None:
        if on_render is None:
            return
        try:
            on_render()
        except Exception:  # noqa: BLE001 — chrome must never break the prompt
            logger.debug("select_option: on_render failed", exc_info=True)

    app: Any = Application(
        layout=Layout(root),
        key_bindings=kb,
        style=style,
        full_screen=False,
        output=output,
        after_render=_after_render,
        erase_when_done=True,
    )
    try:
        chosen = app.run()
    except Exception:
        return _fallback()

    if chosen is None:
        return None
    # The box was ephemeral — echo the choice so the transcript records it.
    console.print(f"[bold cyan]❯[/bold cyan] {options[chosen]}")
    return int(chosen)


def select_options(
    console: Console,
    options: list[str],
    *,
    hint: str | None = None,
    on_render: Callable[[], None] | None = None,
) -> list[int] | None:
    """Choose ANY NUMBER of *options*; return their indices (``None`` = cancel).

    The many-of-N sibling of :func:`select_option`, for questions whose honest
    answer is several of the choices at once — "which entities are affiliated
    with this organization?" is not a one-of-N question, and asking it as one
    forces a wrong answer or none at all.

    Space toggles the highlighted row, Enter confirms the whole set, and ``a``
    toggles everything. Confirming with nothing ticked is a valid answer: it
    means "none of these", which is different from cancelling.

    Same fallback ladder as :func:`select_option` — a non-TTY stdin or a missing
    ``prompt_toolkit`` degrades to a numbered list read through ``input()``,
    accepting comma- or space-separated numbers, so piped and CI runs work.
    """
    if not options:
        return None

    def _fallback() -> list[int] | None:
        if hint:
            console.print(f"[bold]{hint}[/bold]")
        for index, option in enumerate(options, start=1):
            console.print(f"   [cyan]{index}[/cyan]. {option}")
        try:
            raw = console.input(
                f"[bold cyan]Select[/bold cyan] [dim][1-{len(options)}, "
                f"comma-separated; Enter = none][/dim] "
            )
        except EOFError:
            return None
        picked: list[int] = []
        for token in raw.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(options):
                position = int(token) - 1
                if position not in picked:
                    picked.append(position)
        return picked

    if not sys.stdin.isatty():
        return _fallback()
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.output import create_output

        output = create_output()
        if not getattr(output, "responds_to_cpr", True):
            return _fallback()
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
    except Exception:
        return _fallback()

    cursor = {"index": 0}
    ticked: set[int] = set()

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(event: Any) -> None:
        cursor["index"] = (cursor["index"] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _(event: Any) -> None:
        cursor["index"] = (cursor["index"] + 1) % len(options)

    @kb.add("space")
    def _(event: Any) -> None:
        position = cursor["index"]
        ticked.symmetric_difference_update({position})

    @kb.add("a")
    def _(event: Any) -> None:
        if len(ticked) == len(options):
            ticked.clear()
        else:
            ticked.update(range(len(options)))

    @kb.add("enter")
    def _(event: Any) -> None:
        event.app.exit(result=sorted(ticked))

    @kb.add("c-c")
    @kb.add("escape", eager=True)
    def _(event: Any) -> None:
        event.app.exit(result=None)

    for position in range(min(len(options), 9)):

        @kb.add(str(position + 1))
        def _(event: Any, position: int = position) -> None:
            cursor["index"] = position
            ticked.symmetric_difference_update({position})

    def _hline(left: str, right: str):
        def _get() -> list[tuple[str, str]]:
            width = get_app().output.get_size().columns
            return [("class:box", left + "─" * max(0, width - 2) + right)]

        return _get

    def _rows() -> list[tuple[str, str]]:
        width = get_app().output.get_size().columns
        inner = max(0, width - 4)
        fragments: list[tuple[str, str]] = []
        for index, option in enumerate(options):
            focused = index == cursor["index"]
            marker = "❯ " if focused else "  "
            box = "[x]" if index in ticked else "[ ]"
            body = f"{marker}{box} {index + 1}. {option}"[:inner].ljust(inner)
            fragments.append(("class:box", "│ "))
            fragments.append(("class:selected" if focused else "class:option", body))
            fragments.append(("class:box", " │"))
            fragments.append(("", "\n"))
        return fragments

    def _hint_row() -> list[tuple[str, str]]:
        width = get_app().output.get_size().columns
        inner = max(0, width - 4)
        return [
            ("class:box", "│ "),
            ("class:hint", str(hint)[:inner].ljust(inner)),
            ("class:box", " │"),
        ]

    header = [Window(FormattedTextControl(_hint_row), height=1)] if hint else []
    root = HSplit(
        [
            Window(FormattedTextControl(_hline("╭", "╮")), height=1),
            *header,
            Window(FormattedTextControl(_rows), height=len(options)),
            Window(FormattedTextControl(_hline("╰", "╯")), height=1),
            Window(
                FormattedTextControl(
                    [
                        (
                            "class:help",
                            "  ↑/↓ move · space toggle · a all · enter confirm · esc cancel",
                        )
                    ]
                ),
                height=1,
            ),
        ]
    )
    style = Style.from_dict(
        {
            "box": "fg:#5f5f5f",
            "hint": "bold",
            "option": "",
            "selected": "bold ansicyan",
            "help": "fg:#5f5f5f",
        }
    )

    def _after_render(_app: Any) -> None:
        if on_render is None:
            return
        try:
            on_render()
        except Exception:  # noqa: BLE001 — chrome must never break the prompt
            logger.debug("select_options: on_render failed", exc_info=True)

    app: Any = Application(
        layout=Layout(root),
        key_bindings=kb,
        style=style,
        full_screen=False,
        output=output,
        after_render=_after_render,
        erase_when_done=True,
    )
    try:
        chosen = app.run()
    except Exception:
        return _fallback()

    if chosen is None:
        return None
    picked = [int(index) for index in chosen]
    # The box was ephemeral — echo the set so the transcript records it.
    console.print(
        f"[bold cyan]❯[/bold cyan] {', '.join(options[i] for i in picked) if picked else 'none'}"
    )
    return picked
