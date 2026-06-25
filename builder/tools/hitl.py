"""Human-in-the-Loop interaction tools for the ISA-Tox RO-Crate Builder.

Provides a :class:`HumanInterface` protocol so frontends (Streamlit, FastAPI,
…) can be injected into :class:`~builder.engine.AgentEngine` without
monkeypatching. The default :class:`SimulatedHumanInterface` reproduces the
previous non-interactive stub behaviour (auto-approve, skip-input). The
module-level :func:`present_to_human` / :func:`request_input` functions remain
as thin wrappers over a shared default simulator for backward compatibility.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

logger = logging.getLogger(__name__)


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


class ConsoleHumanInterface:
    """A REAL interactive HITL interface that prompts on the terminal (stdin).

    This is the CLI frontend the **default interactive build path** runs behind
    (`main.py --interactive` → `run_interactive_build`, AGENTS.md §14.6.1). Unlike
    :class:`SimulatedHumanInterface` it is ``is_interactive = True``, so the
    guidance tail actually runs and routes its ask-user prompts / draft
    confirmations to the user via ``input()``.

    A scan-root escalation still routes through the user — they are the only
    legitimate approver for widening filesystem access (#197); a non-affirmative
    answer denies it (fail-closed). An empty answer to an input request is a skip.
    Reading from a closed / non-tty stdin (``EOFError``) is treated as decline /
    skip so the loop never hangs or crashes on a piped invocation.
    """

    is_interactive: bool = True

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        """Show *context* + *options* and read an approve/reject decision."""
        print(context)
        if options:
            print(f"Options: {', '.join(options)}")
        suffix = " [y/N]: " if purpose == SCAN_ROOT_PURPOSE else " [Y/n]: "
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
        """Prompt the user for a value; an empty answer (or EOF) is a skip."""
        print(prompt)
        try:
            value = input(f"({field_type}) > ").strip()
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
