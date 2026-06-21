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


@runtime_checkable
class HumanInterface(Protocol):
    """Dependency-injection point for human-in-the-loop interaction.

    Implementations adapt the agent's HITL requests to a concrete frontend
    (CLI prompt, Streamlit widget, FastAPI round-trip, test double, …).
    """

    def present(
        self, context: str, options: list[str] | None = None
    ) -> HumanResponse:
        """Present content to the human and return their decision."""
        ...

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        """Request a specific input value from the human."""
        ...


class SimulatedHumanInterface:
    """Default non-interactive interface: auto-approves and skips input.

    Reproduces the previous stub behaviour so headless/batch runs proceed
    without blocking on a real user.
    """

    def present(
        self, context: str, options: list[str] | None = None
    ) -> HumanResponse:
        """Log the presentation and return a simulated-approved response."""
        logger.info("HITL presentation: %s", context)
        if options:
            logger.info("HITL options: %s", options)
        return {"action": "approved", "comments": None, "edits": None}

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        """Log the request and return a skip response."""
        logger.info("HITL input request: %s (type=%s)", prompt, field_type)
        return {"value": None, "skipped": True}


# Shared default simulator backing the module-level convenience functions.
_default_interface: HumanInterface = SimulatedHumanInterface()


def present_to_human(
    context: str,
    options: list[str] | None = None,
) -> HumanResponse:
    """Present content to the human via the default simulated interface.

    Backward-compatible wrapper; new code should inject a
    :class:`HumanInterface` into :class:`~builder.engine.AgentEngine` instead.
    """
    return _default_interface.present(context, options)


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
    "HumanInterface",
    "HumanResponse",
    "InputResponse",
    "SimulatedHumanInterface",
    "present_to_human",
    "request_input",
]
