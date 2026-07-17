"""Eval-only HITL interface: approve trusted-corpus scan roots (fair A/B).

The production default :class:`~builder.tools.hitl.SimulatedHumanInterface` is
fail-closed on scan-root escalations: a headless simulator must never be the
approver for widening filesystem access (the #197/#198 guard). That is correct in
production, but it makes the A/B eval *unfair*.

The eval compares the ReAct and pipeline build modes over the same in-repo corpus
fixtures. The pipeline arm only ever touches the pre-approved ``--input`` directory
and is never refused; the ReAct arm, exploring, asks to scan a fixture directory
outside that dir and is hard-refused ("Refusing scan of unapproved path",
:meth:`builder.engine.AgentEngine._authorize_scan_root`). The A/B then measures a
*security handicap*, not the architectures.

:class:`TrustedCorpusHumanInterface` removes that asymmetry — and *only* for the
A/B. The corpus fixtures are vetted, in-repo directories, so approving a scan of
one is safe HERE and nowhere else. It is EVAL-ONLY: never wire it behind a real,
user-facing build. Keeping it under ``eval/`` (not ``builder/tools/``) makes that
boundary explicit — nothing in production can reach it.
"""

from __future__ import annotations

import logging

from builder.tools.hitl import (
    SCAN_ROOT_PURPOSE,
    HumanResponse,
    SimulatedHumanInterface,
)

logger = logging.getLogger(__name__)


class TrustedCorpusHumanInterface(SimulatedHumanInterface):
    """Headless eval interface that APPROVES scan-root escalations (eval-only).

    Identical to :class:`~builder.tools.hitl.SimulatedHumanInterface` except:

    * ``is_interactive`` is ``True``. The engine's ``_authorize_scan_root`` fails
      closed on a *non-interactive* human BEFORE it ever consults ``present``
      (:mod:`builder.engine`), so the interface must present as interactive for its
      approval to be reached. This does **not** trigger the interactive guidance
      tail: the eval factories drive ``run_pipeline`` / the ReAct graph directly,
      never ``run_interactive_build`` (AGENTS.md §14.6.1), so the ``is_interactive``
      gate there is never crossed.
    * ``present`` APPROVES scan-root escalations instead of denying them, so the
      ReAct arm can read the trusted corpus fixtures the pipeline arm already can.

    ``request_input`` is inherited unchanged (always skips), so nothing blocks on a
    real stdin.
    """

    is_interactive: bool = True

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        """Approve every checkpoint, INCLUDING scan-root escalations.

        The corpus fixtures are trusted, in-repo directories, so approving a scan
        of one keeps the A/B fair. Never use this outside the eval.
        """
        logger.info("HITL (trusted-corpus) auto-approve: %s", context)
        if options:
            logger.info("HITL options: %s", options)
        if purpose == SCAN_ROOT_PURPOSE:
            logger.info("Approving trusted-corpus scan root (eval-only): %s", context)
        return {"action": "approved", "comments": None, "edits": None}
