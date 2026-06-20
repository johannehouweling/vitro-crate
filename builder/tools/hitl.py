"""Human-in-the-Loop interaction tools for the ISA-Tox RO-Crate Builder.

Provides primitives for presenting content to the user and incorporating
their feedback into the CrateState workflow.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def present_to_human(
    context: str,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """Present content to the human user and get a response.

    In the default (non-interactive) mode, this logs the presentation
    and returns a simulated-approved response. In production, this would
    be overridden by a UI callback.

    Args:
        context: Human-readable description of what's being presented.
        options: Optional list of response buttons (e.g. ["Approve", "Edit", "Reject"]).

    Returns:
        A dict with keys:
            action: One of "approved", "edited", "rejected", "skipped".
            comments: Free-text comments from the user.
            edits: Dict of field edits (only present if action == "edited").
    """
    logger.info("HITL presentation: %s", context)
    if options:
        logger.info("HITL options: %s", options)

    # Simulate approval — in production this would block waiting for user input
    return {
        "action": "approved",
        "comments": "",
    }


def request_input(
    prompt: str,
    field_type: str = "text",
) -> dict[str, Any]:
    """Request a specific input from the user.

    In the default mode, logs the request and returns a skip response.

    Args:
        prompt: The question or prompt to present to the user.
        field_type: Type of input expected ("text", "identifier", "select").

    Returns:
        A dict with keys:
            value: The user's input, or None if skipped.
            skipped: True if the user chose to skip.
    """
    logger.info("HITL input request: %s (type=%s)", prompt, field_type)
    return {"value": None, "skipped": True}


__all__ = ["present_to_human", "request_input"]