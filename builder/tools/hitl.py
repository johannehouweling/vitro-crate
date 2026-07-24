"""Human-in-the-Loop interaction tools for the ISA-Tox RO-Crate Builder.

Provides a :class:`HumanInterface` protocol so frontends (Streamlit, FastAPI,
…) can be injected into :class:`~builder.engine.AgentEngine` without
monkeypatching. The default :class:`SimulatedHumanInterface` reproduces the
previous non-interactive stub behaviour (auto-approve, skip-input). The
module-level :func:`present_to_human` / :func:`request_input` functions remain
as thin wrappers over a shared default simulator for backward compatibility.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

logger = logging.getLogger(__name__)


# --- console animation suspension -------------------------------------------
# A long-running CLI animation (e.g. the legacy agent loop's "thinking" spinner,
# a Rich ``Live`` region driven by a daemon thread) repaints the terminal
# continuously, which clobbers a blocking ``input()`` prompt — the user cannot
# read or answer a HITL question (scan-root approval, ask-user) while it ticks.
# The animation registers itself here; the console HITL prompts suspend it for
# the duration of ``input()``. Frontend-agnostic: a non-CLI HumanInterface never
# registers anything and these become no-ops.
_active_animation: Any = None
_animation_lock = threading.Lock()


def register_console_animation(animation: Any) -> None:
    """Register the active terminal animation so console prompts can pause it.

    ``animation`` must expose ``pause()`` and ``resume()``. The most recent
    registration wins (CLI animations are not nested).
    """
    global _active_animation
    with _animation_lock:
        _active_animation = animation


def unregister_console_animation(animation: Any) -> None:
    """Clear the registered animation, but only if it is *animation*."""
    global _active_animation
    with _animation_lock:
        if _active_animation is animation:
            _active_animation = None


@contextlib.contextmanager
def suspend_console_animation() -> Iterator[None]:
    """Pause any registered console animation for the duration of the block.

    Best-effort: no registered animation is a no-op, and a ``pause``/``resume``
    that raises is logged but never propagated — UI chrome must not break a HITL
    prompt. ``resume`` always runs, even if the body raises.
    """
    with _animation_lock:
        animation = _active_animation
    if animation is not None:
        try:
            animation.pause()
        except Exception:  # noqa: BLE001 — never let UI chrome break a prompt
            logger.debug("console animation pause failed", exc_info=True)
    try:
        yield
    finally:
        if animation is not None:
            try:
                animation.resume()
            except Exception:  # noqa: BLE001 — never let UI chrome break a prompt
                logger.debug("console animation resume failed", exc_info=True)


class HumanResponse(TypedDict):
    """Response from presenting content to a human for review.

    Attributes:
        action: One of "approved", "edited", "rejected", "skipped".
        comments: Free-text comments from the user, or None.
        edits: Dict of field edits (when action == "edited"), or None.
    """

    action: Literal["approved", "edited", "rejected", "skipped"]
    comments: str | None
    edits: dict | None


class InputResponse(TypedDict):
    """Response from requesting a specific input value from a human.

    Attributes:
        value: The user's input, or None if skipped.
        skipped: True if the user chose to skip.
    """

    value: Any | None
    skipped: bool


# Sentinel ``purpose`` value marking a request to approve a NEW filesystem
# scan root. The default/simulated interface must DENY these — it can never be
# the approver for filesystem access (fail-closed, #197).
SCAN_ROOT_PURPOSE = "scan_root"


@runtime_checkable
class HumanInterface(Protocol):
    """Dependency-injection point for human-in-the-loop interaction.

    Implementations adapt the agent's HITL requests to a concrete frontend
    (CLI prompt, Streamlit widget, FastAPI round-trip, test double, …).

    An **optional** ``is_interactive: bool`` attribute is the single signal the
    interactive build path uses to decide whether to run the HITL guidance tail
    after the automated pipeline (AGENTS.md §14.6.1): a frontend backed by a REAL
    user sets it ``True``; the headless :class:`SimulatedHumanInterface` (and the
    A/B eval, batch runs, and tests that use it) leaves it ``False`` so guidance is
    never invoked non-interactively. It is **deliberately NOT a required Protocol
    member** — making it required would force every existing adapter / test double
    to declare it. Read it via the fail-closed :func:`is_interactive` helper, which
    treats an interface that omits it (or no interface at all) as non-interactive.
    """

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        """Present content to the human and return their decision.

        *purpose* optionally classifies the request (e.g. ``"scan_root"`` for a
        request to approve a new filesystem scan root). Implementations may use
        it to apply stricter handling to security-sensitive escalations.
        """
        ...

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        """Request a specific input value from the human."""
        ...


class SimulatedHumanInterface:
    """Default non-interactive interface: auto-approves and skips input.

    Reproduces the previous stub behaviour so headless/batch runs proceed
    without blocking on a real user — EXCEPT for scan-root escalations, which
    it denies: the simulator can never be the approver for filesystem access
    (fail-closed, #197).

    ``is_interactive`` is ``False`` — this is the headless default, so the
    interactive build path (AGENTS.md §14.6) never runs the HITL guidance tail
    behind it (the A/B eval and batch runs stay automated-only).
    """

    is_interactive: bool = False

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        """Log the presentation and return a simulated decision.

        Benign checkpoints (entity review, etc.) auto-approve as before. A
        scan-root escalation (``purpose == "scan_root"``) is DENIED — the
        non-interactive default must not silently widen filesystem access.
        """
        logger.info("HITL presentation: %s", context)
        if options:
            logger.info("HITL options: %s", options)
        if purpose == SCAN_ROOT_PURPOSE:
            logger.warning(
                "Denying simulated approval of a new scan root (fail-closed): %s",
                context,
            )
            return {"action": "rejected", "comments": None, "edits": None}
        return {"action": "approved", "comments": None, "edits": None}

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        """Log the request and return a skip response."""
        logger.info("HITL input request: %s (type=%s)", prompt, field_type)
        return {"value": None, "skipped": True}


def _default_console_prompt(field_type: str) -> str:
    """Plain-terminal input reader — the default when no UI box is injected.

    Kept here (not in :mod:`builder.agents.ui`) so ``hitl`` stays independent of
    the UI layer: importing ``builder.agents.ui`` from a ``builder.tools`` module
    would form an ``agents → tools → agents`` import cycle. The CLI injects the
    shared rounded ``❯`` box via ``ConsoleHumanInterface(prompt_func=...)`` instead.
    """
    return input(f"({field_type}) > ")


class ConsoleHumanInterface:
    """A REAL interactive HITL interface that prompts on the terminal (stdin).

    This is the CLI frontend the **default interactive build path** runs behind
    (`main.py --interactive` → `run_interactive_build`, AGENTS.md §14.6.1). Unlike
    :class:`SimulatedHumanInterface` it is ``is_interactive = True``, so the
    guidance tail actually runs and routes its ask-user prompts / draft
    confirmations to the user.

    The free-text prompt is read through an injectable ``prompt_func`` (a
    ``field_type -> text`` reader). It defaults to a plain ``input()`` so this
    module never imports the UI layer (avoiding an ``agents → tools → agents``
    cycle); the CLI injects :func:`builder.agents.ui.boxed_input` so the pipeline's
    HITL prompt renders through the SAME rounded box the ReAct arm uses (#344).

    A scan-root escalation still routes through the user — they are the only
    legitimate approver for widening filesystem access (#197); a non-affirmative
    answer denies it (fail-closed). An empty answer to an input request is a skip.
    Reading from a closed / non-tty stdin (``EOFError``) is treated as decline /
    skip so the loop never hangs or crashes on a piped invocation.
    """

    is_interactive: bool = True

    def __init__(self, prompt_func: Callable[[str], str] | None = None) -> None:
        """Build the interface, optionally injecting the free-text prompt reader.

        Args:
            prompt_func: A ``field_type -> entered-text`` reader used by
                :meth:`request_input`. Defaults to :func:`_default_console_prompt`
                (a plain ``input()``). The CLI passes a reader bound to the shared
                rounded box (:func:`builder.agents.ui.boxed_input`).
        """
        self._read: Callable[[str], str] = prompt_func or _default_console_prompt

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        """Show *context* + *options* and read an approve/reject decision."""
        suffix = " [y/N]: " if purpose == SCAN_ROOT_PURPOSE else " [Y/n]: "
        # Suspend any active terminal spinner so the prompt is readable and stdin
        # is not fighting a Rich Live repaint (legacy loop scan-root approval).
        with suspend_console_animation():
            print(context)
            if options:
                print(f"Options: {', '.join(options)}")
            try:
                answer = input(f"Approve?{suffix}").strip().lower()
            except EOFError:
                answer = ""
        if purpose == SCAN_ROOT_PURPOSE:
            # Fail-closed: a new scan root requires an explicit affirmative.
            approved = answer in ("y", "yes")
        else:
            approved = answer in ("", "y", "yes")
        action = "approved" if approved else "rejected"
        return {"action": action, "comments": None, "edits": None}

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        """Prompt the user for a value; an empty answer (or EOF) is a skip.

        Reads via the injected ``prompt_func`` (the shared rounded box in the CLI,
        a plain ``input()`` otherwise), suspending any active terminal spinner so
        stdin is not fighting a Rich Live repaint.
        """
        with suspend_console_animation():
            print(prompt)
            try:
                value = self._read(field_type).strip()
            except EOFError:
                value = ""
        if not value:
            return {"value": None, "skipped": True}
        return {"value": value, "skipped": False}


def is_interactive(human: HumanInterface | None) -> bool:
    """Whether *human* is a REAL interactive frontend (vs headless/simulated).

    The single gate the interactive build path uses to decide whether to run the
    HITL guidance tail after the automated pipeline (AGENTS.md §14.6). Reads the
    optional :attr:`HumanInterface.is_interactive` signal **fail-closed**: a
    ``None`` interface (a headless engine), an interface that does not declare the
    attribute, or one that declares it falsy is treated as **non-interactive**.
    Only an interface that explicitly sets ``is_interactive`` truthy — a frontend
    backed by a real user — returns ``True``.
    """
    return bool(getattr(human, "is_interactive", False))


# Shared default simulator backing the module-level convenience functions.
_default_interface: HumanInterface = SimulatedHumanInterface()


def present_to_human(
    context: str,
    options: list[str] | None = None,
    purpose: str | None = None,
) -> HumanResponse:
    """Present content to the human via the default simulated interface.

    Backward-compatible wrapper; new code should inject a
    :class:`HumanInterface` into :class:`~builder.engine.AgentEngine` instead.
    """
    return _default_interface.present(context, options, purpose)


def request_input(
    prompt: str,
    field_type: str = "text",
) -> InputResponse:
    """Request input from the human via the default simulated interface.

    Backward-compatible wrapper; new code should inject a
    :class:`HumanInterface` into :class:`~builder.engine.AgentEngine` instead.
    """
    return _default_interface.request_input(prompt, field_type)


__all__ = [
    "SCAN_ROOT_PURPOSE",
    "ConsoleHumanInterface",
    "HumanInterface",
    "HumanResponse",
    "InputResponse",
    "SimulatedHumanInterface",
    "is_interactive",
    "present_to_human",
    "request_input",
]
