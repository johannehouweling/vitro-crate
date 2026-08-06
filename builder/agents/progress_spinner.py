"""The shared live progress spinner for the interactive builds (#266, #344).

The DEFAULT ``--interactive`` (pipeline) build prints static phase lines (#253),
so the ~tens-of-seconds deterministic spine looked frozen. :class:`ProgressSpinner`
gives it a live Rich spinner: an animated dots spinner with a rotating funny
toxicology-themed phrase, the currently-running tool/phase, and elapsed seconds,
all updating in place.

**Both build arms share this one spinner (#344).** The deterministic pipeline drives
it from ``engine.on_tool_event`` plus its per-phase strings; the legacy ReAct loop
drives it from LangChain tool-event callbacks (``_ToolSpinnerCallback`` in
``builder/agents/react/agent_loop.py``). This module is the single source of both the
spinner class and :data:`TOX_SPINNER_PHRASES` — neither arm keeps a private copy.

It is a reusable context manager driven by a daemon tick thread, rendering::

    <funny phrase>… (<elapsed>s) · <current op>

**HITL safety.** The guidance tail asks the user questions via ``input()`` mid-build;
a Rich ``Live`` repaint would clobber that prompt. The spinner registers itself as the
active console animation (``builder.tools.hitl.register_console_animation``) on enter and
unregisters on exit, so a console HITL prompt — which wraps its ``input()`` in
``suspend_console_animation`` (#239) — calls :meth:`pause` (tear down the Live region,
stop ticking) for the duration of the prompt and :meth:`resume` afterwards. The tick
thread skips while paused.
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable
from time import monotonic
from typing import Any

from builder.tools.hitl import (
    register_console_animation,
    unregister_console_animation,
)

logger = logging.getLogger(__name__)

__all__ = ["ProgressSpinner", "TOX_SPINNER_PHRASES"]

# Animation frames for the delegated (footer-painted) line. Rich's "dots"
# spinner draws these itself in Live mode; the footer needs them spelled out.
_DELEGATED_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Characters of streamed reply text kept on the spinner line. Sized to leave
# room for the phrase, the elapsed counter and the quotes on an 80-column
# terminal; the footer truncates anything wider anyway.
_PREVIEW_WIDTH = 40


# The single toxicology-themed phrase list, shared by both build arms (#344).
# ``ProgressSpinner`` picks one at random when no explicit phrase is given.
TOX_SPINNER_PHRASES: list[str] = [
    "intoxicating",
    "ro-crating",
    "FAIR-ifying",
    "culturing the cells",
    "calibrating the dose",
    "annotating ontologies",
    "resolving compounds",
    "reticulating gels",
    "buffering the buffer",
    "negotiating with reviewer 2",
    "summoning the IC50",
    "vortexing thoughtfully",
    "rehydrating the lyophilate",
    "titrating responsibly",
    "resuspending the pellet",
    "autoclaving doubt",
    "thawing the -80",
    "centrifuging the chaos",
    "pipetting precisely",
    "decoding the SDS",
    "labelling 'compound X'",
    "counting colonies, twice",
    "manifesting p<0.05",
    "consulting the FAIR gods",
    "interoperating",
    "double-gloving",
    "minting identifiers",
    "validating the pyramid",
    "wrangling SHACL shapes",
    "linking provenance",
]


class ProgressSpinner:
    """A Rich status spinner that ticks elapsed seconds and shows the current op.

    Used as a context manager around the deterministic interactive build. A daemon
    thread refreshes the rendered line (so the elapsed-seconds counter advances even
    while a long phase runs); :meth:`set_current` swaps in the currently-running
    tool/phase. On enter it registers with the hitl animation registry so a HITL
    prompt can :meth:`pause`/:meth:`resume` it via ``suspend_console_animation``.

    Colour convention: green = working, dim = elapsed/meta, cyan = the current op.

    **Non-TTY safety (CI / piped).** A live animation only makes sense on a real
    terminal. When the console is NOT a terminal (``console.is_terminal`` is
    falsey — CI, a pipe, a captured/redirected stream), the spinner is built in a
    *silent* mode: it opens **no** Rich ``Live`` region (so no Rich background
    refresh thread) and starts **no** daemon tick thread, and every public method
    (:meth:`set_current` / :meth:`pause` / :meth:`resume` / :meth:`__enter__` /
    :meth:`__exit__`) is a cheap, safe no-op. This keeps the headless / CI path
    free of background threads to start, stop and join under a ``--timeout``
    thread-dumper, so it can never hang there; on a real TTY it animates exactly
    as before. A console without an ``is_terminal`` attribute (e.g. a test fake)
    defaults to the animated path.
    """

    def __init__(
        self,
        console: Any | None = None,
        phrase: str | None = None,
        *,
        tick_interval: float = 0.5,
        activity_sink: Callable[[str | None], None] | None = None,
    ) -> None:
        """Build a spinner.

        Args:
            console: A Rich ``Console`` (or a compatible object exposing
                ``status(...)``). Defaults to a fresh ``rich.console.Console``.
            phrase: The funny phrase to show. Defaults to a random pick from
                :data:`TOX_SPINNER_PHRASES`.
            tick_interval: Seconds between elapsed-time repaints by the daemon
                thread. Kept configurable so tests can tick quickly.
            activity_sink: When given, the spinner **delegates** its display:
                it opens no Rich ``Live`` region and instead pushes its rendered
                line to this callback (``None`` when it finishes), which the
                pinned footer paints on its own reserved row. Two wins beyond
                tidiness — the working line stops drifting up the transcript
                with the output it interleaves with, and since the footer sits
                OUTSIDE the terminal's scrolling region it cannot clobber a
                prompt, so nothing has to be suspended to read stdin. Without a
                sink the spinner behaves exactly as before.
        """
        if console is None:
            from rich.console import Console

            console = Console()
        self._console = console
        self._phrase = phrase or random.choice(TOX_SPINNER_PHRASES)
        self._current: str | None = None
        self._preview: str | None = None
        self._preview_buffer = ""
        self._tick_interval = tick_interval
        self._sink = activity_sink
        self._frame = 0
        self._start = monotonic()
        self._stop = threading.Event()
        # Set while a HITL prompt owns the terminal: the tick thread must not
        # repaint the Rich Live region over the prompt (else stdin is unusable).
        self._paused = threading.Event()
        # Only animate on a real terminal. A non-terminal output (CI, a pipe, a
        # captured stream) gets the silent no-op mode: no Live region, no Rich
        # refresh thread, no daemon tick thread — nothing to hang on teardown.
        # A console missing ``is_terminal`` (a test fake) defaults to animated.
        self._active: bool = bool(getattr(console, "is_terminal", True))
        if self._sink is not None:
            # Delegated: the footer owns the pixels, so there is no Live region
            # to build — but the tick thread still runs, because the elapsed
            # counter and the spinner frame have to keep moving.
            self._status: Any | None = None
            self._thread: threading.Thread | None = threading.Thread(
                target=self._tick, daemon=True
            )
        elif self._active:
            self._status = console.status(
                self._render(), spinner="dots", spinner_style="green"
            )
            self._thread = threading.Thread(target=self._tick, daemon=True)
        else:
            self._status = None
            self._thread = None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self) -> str:
        """Render the spinner line: phrase, elapsed seconds, and the current op.

        A running tool wins over the reply tail: they never overlap in time
        (the model finishes writing before a tool starts), and when both are
        somehow set the concrete tool is the more informative of the two.
        """
        elapsed = int(monotonic() - self._start)
        line = f"[green]{self._phrase}…[/green] [dim]({elapsed}s)[/dim]"
        if self._current:
            line += f"  [dim]·[/dim] [cyan]{self._current}[/cyan]"
        elif self._preview:
            line += f'  [dim]·[/dim] [dim]writing:[/dim] [white]"{self._preview}"[/white]'
        return line

    def _render_delegated(self) -> str:
        """The delegated line — the animation frame Rich would otherwise draw.

        ``console.status`` supplies the turning glyph in Live mode; when the
        footer paints the line instead, the frame has to come from here.
        """
        glyph = _DELEGATED_FRAMES[self._frame % len(_DELEGATED_FRAMES)]
        return f"[green]{glyph}[/green] {self._render()}"

    def _publish(self) -> None:
        """Push the current line to the activity sink (best-effort)."""
        if self._sink is None:
            return
        try:
            self._sink(self._render_delegated())
        except Exception:  # noqa: BLE001 — UI chrome must never break the build
            logger.debug("spinner publish failed", exc_info=True)

    def _tick(self) -> None:
        """Daemon loop: repaint the elapsed time roughly every tick_interval.

        Skips entirely while paused (a HITL prompt owns the terminal). A repaint
        failure (e.g. the status was torn down) stops the loop cleanly.
        """
        while not self._stop.wait(self._tick_interval):
            if self._paused.is_set():
                continue
            if self._sink is not None:
                self._frame += 1
                self._publish()
                continue
            status = self._status
            if status is None:  # silent mode never starts this thread; belt-and-braces
                break
            try:
                status.update(self._render())
            except Exception:  # noqa: BLE001 — a torn-down Live must not crash the thread
                break

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_current(self, text: str | None) -> None:
        """Show (or clear, with ``None``) the currently-running tool/phase.

        Records the op and repaints immediately — unless paused, in which case the
        op is remembered but the Live region is left alone so a HITL prompt stays
        readable (the next resume/tick picks it up). On a non-TTY (silent mode)
        the op is still remembered but there is no Live region to repaint.
        """
        self._current = text
        if self._sink is not None:
            if not self._paused.is_set():
                self._publish()
            return
        if not self._active or self._status is None:
            return  # silent mode: remember the op, paint nothing
        if self._paused.is_set():
            return
        try:
            self._status.update(self._render())
        except Exception:  # noqa: BLE001 — UI chrome must never break the build
            logger.debug("spinner set_current: status.update failed", exc_info=True)

    def append_preview(self, token: str) -> None:
        """Append a streamed token to the live reply tail.

        Deliberately does NOT repaint: a fast stream would otherwise mean a
        terminal write per token. The tick thread picks the text up on its next
        pass, so the tail advances at the animation rate whatever the token rate.
        """
        if not token:
            return
        self._preview_buffer += token
        # Keep a moving window of the most recent characters: a fixed head
        # would freeze after the first few words and stop reading as live.
        window = self._preview_buffer.replace("\n", " ")[-_PREVIEW_WIDTH:]
        self._preview = window.lstrip()

    def set_preview(self, text: str | None) -> None:
        """Replace (``None`` clears) the reply tail shown while the model writes."""
        self._preview_buffer = text or ""
        self._preview = (text or "").replace("\n", " ")[-_PREVIEW_WIDTH:] or None

    def pause(self) -> None:
        """Tear down the Live region and stop ticking so a HITL prompt is clean.

        Called (via :func:`builder.tools.hitl.suspend_console_animation`) when the
        guidance tail needs ``input()`` mid-build. Best-effort and idempotent. A
        no-op in silent mode (there is no Live region owning the terminal).
        """
        self._paused.set()
        if self._sink is not None:
            # Delegated: the footer lives outside the scrolling region, so it
            # never overwrites a prompt. Freeze the line (the elapsed counter
            # stops while the user reads) but leave it visible.
            return
        if not self._active or self._status is None:
            return
        try:
            self._status.stop()
        except Exception:  # noqa: BLE001
            logger.debug("spinner pause: status.stop failed", exc_info=True)

    def resume(self) -> None:
        """Restart the Live region after a HITL prompt completes.

        A no-op in silent mode (there was no Live region to restart).
        """
        if self._sink is not None:
            self._paused.clear()
            self._publish()
            return
        if not self._active or self._status is None:
            self._paused.clear()
            return
        try:
            self._status.start()
        except Exception:  # noqa: BLE001
            logger.debug("spinner resume: status.start failed", exc_info=True)
        self._paused.clear()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ProgressSpinner":
        # In silent mode (non-TTY) there is no Live region and no tick thread to
        # start — just register so a HITL prompt's suspend/resume stays valid.
        if self._sink is not None:
            if self._thread is not None:
                self._thread.start()
            self._publish()
        elif self._active and self._status is not None and self._thread is not None:
            self._status.__enter__()
            self._thread.start()
        register_console_animation(self)
        return self

    def __exit__(self, *exc: Any) -> None:
        unregister_console_animation(self)
        if self._sink is not None:
            # Stop ticking and clear the footer's activity row — the work this
            # line described is over, so leaving it up would misreport.
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=1.0)
            try:
                self._sink(None)
            except Exception:  # noqa: BLE001 — teardown must never raise
                logger.debug("spinner exit: sink clear failed", exc_info=True)
            return
        # Silent mode: nothing was started, so nothing to stop or join.
        if not self._active:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._status is None:
            return
        try:
            self._status.__exit__(*exc)
        except Exception:  # noqa: BLE001 — teardown must never raise on exit
            logger.debug("spinner exit: status.__exit__ failed", exc_info=True)
