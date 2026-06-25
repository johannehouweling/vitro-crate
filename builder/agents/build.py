"""The interactive hybrid build path (Issue #179 — completes the §14 loop).

This is the **interactive entrypoint** that joins the two halves of the §14.6
hybrid build loop into the end-to-end sequence a real user runs:

    Extract → Materialize → Assess → Auto-resolve   ──►   Guidance
    └──────────── run_pipeline (AUTOMATED) ─────────┘     run_guidance (HITL)

:func:`run_interactive_build` runs the **automated** deterministic pipeline
(:func:`builder.agents.pipeline.run_pipeline`) and then — *only* when a REAL
interactive :class:`~builder.tools.hitl.HumanInterface` is present — runs the
HITL gap-resolution tail (:func:`builder.agents.guidance.run_guidance`) so the
user is guided through the gaps the deterministic path could not close on its own.

**Why the split is deliberate.** ``run_pipeline`` stays *guidance-free*: it is the
automated build the A/B eval drives non-interactively (``--arch pipeline``), so it
must be a clean automated-vs-automated comparison with the ReAct path. Guidance is
HITL and would block on a real user, so it belongs only here, in the interactive
tail, gated on :func:`builder.tools.hitl.is_interactive`. The headless
:class:`~builder.tools.hitl.SimulatedHumanInterface` (the eval, batch runs, tests)
reports ``is_interactive == False``, so this entrypoint then degrades to *exactly*
``run_pipeline`` alone — guidance is never invoked behind a simulated human.

Both the pipeline and guidance runners are **injected** (defaulting to the real
functions) so the wiring is unit-testable with no SHACL / no LLM / no network.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from builder.tools.hitl import is_interactive

if TYPE_CHECKING:
    from builder.engine import AgentEngine
    from builder.tools.hitl import HumanInterface

logger = logging.getLogger(__name__)

__all__ = ["run_interactive_build", "format_guidance_summary"]

# A pipeline_runner runs the automated deterministic spine once over an engine,
# mutating engine.state and returning the spine's result dict. A guidance_runner
# runs the HITL gap-resolution loop over the engine + a human, returning its
# summary dict. Both are injected so the wiring is testable without SHACL/LLM.
PipelineRunner = Callable[["AgentEngine"], dict[str, Any]]
GuidanceRunner = Callable[..., dict[str, Any]]

# Default no-op output channel: discard. The CLI passes ``print`` (or a console
# writer); tests pass a list's ``append`` to capture the surfaced summary.
OutputChannel = Callable[[str], Any]


def run_interactive_build(
    engine: AgentEngine,
    *,
    pipeline_runner: PipelineRunner | None = None,
    guidance_runner: GuidanceRunner | None = None,
    output: OutputChannel | None = None,
) -> dict[str, Any]:
    """Run the automated pipeline, then the HITL guidance tail when interactive.

    The engine MUST already be :meth:`~builder.engine.AgentEngine.initialize`-d.
    The automated pipeline always runs. The HITL guidance loop runs **iff** the
    engine's :class:`~builder.tools.hitl.HumanInterface` is interactive
    (:func:`builder.tools.hitl.is_interactive`) — a real user's frontend — so the
    non-interactive / simulated path (the A/B eval, batch, tests) runs the
    automated pipeline alone and ``run_guidance`` is never invoked. When guidance
    runs, a concise summary of its results is surfaced via *output*.

    Args:
        engine: An initialized engine. Its ``human_interface`` decides whether
            the guidance tail runs; ``run_guidance`` mutates ``engine.state`` in
            place through the existing tools (never hand-rolled JSON-LD).
        pipeline_runner: Injected automated-build runner; defaults to the real
            :func:`builder.agents.pipeline.run_pipeline` (kept guidance-free).
        guidance_runner: Injected HITL runner; defaults to the real
            :func:`builder.agents.guidance.run_guidance`.
        output: Sink for the human-readable guidance summary (e.g. ``print`` or a
            console writer). Defaults to a no-op (nothing is emitted). Only the
            guidance summary is surfaced here — the pipeline's own output is the
            caller's concern.

    Returns:
        ``{"pipeline": <run_pipeline result>, "guidance": <run_guidance result or
        None>}`` — ``guidance`` is ``None`` exactly when the path was
        non-interactive and the tail was skipped.
    """
    pipeline_runner = pipeline_runner or _default_pipeline_runner()
    pipeline_result = pipeline_runner(engine)

    human: HumanInterface | None = getattr(engine, "human_interface", None)
    if not is_interactive(human):
        # Headless / simulated: run the automated pipeline ALONE so the A/B stays
        # a clean automated-vs-automated comparison. No guidance, no summary.
        logger.debug("Non-interactive build: skipping the HITL guidance tail")
        return {"pipeline": pipeline_result, "guidance": None}

    guidance_runner = guidance_runner or _default_guidance_runner()
    guidance_result = guidance_runner(engine, human)

    emit = output or (lambda _msg: None)
    emit(format_guidance_summary(guidance_result))

    return {"pipeline": pipeline_result, "guidance": guidance_result}


def format_guidance_summary(guidance_result: dict[str, Any] | None) -> str:
    """Render a concise, human-readable summary of a guidance run.

    Surfaces what the HITL tail did — gaps *resolved*, gaps *asked* about, gaps
    *remaining* per tier — and the final per-layer (``base`` / ``isa`` / ``tox``)
    conformance, so the user sees the build's state after guidance at a glance.
    A ``None`` result (guidance never ran, e.g. a non-interactive build) yields a
    short, non-crashing message.
    """
    if not guidance_result:
        return "No interactive guidance ran (headless build)."

    resolved = guidance_result.get("resolved") or []
    asked = guidance_result.get("asked") or []
    remaining = guidance_result.get("remaining_gaps") or {}
    conformance = guidance_result.get("conformance") or {}
    rounds = guidance_result.get("rounds", 0)

    must = remaining.get("must_open", 0)
    should = remaining.get("should_open", 0)
    may = remaining.get("may_open", 0)

    def _mark(layer: str) -> str:
        return "pass" if conformance.get(layer) else "fail"

    lines = [
        "Guidance complete:",
        f"  resolved: {len(resolved)} gap(s)",
        f"  asked:    {len(asked)} gap(s)",
        f"  remaining gaps: {must} MUST / {should} SHOULD / {may} MAY",
        (
            f"  conformance: base={_mark('base')} "
            f"isa={_mark('isa')} tox={_mark('tox')}"
        ),
        f"  rounds: {rounds}",
    ]
    return "\n".join(lines)


def _default_pipeline_runner() -> PipelineRunner:
    """The real automated spine, imported lazily so this module stays light.

    Importing :mod:`builder.agents.pipeline` is cheap and langchain-free (the
    leaf imports are themselves lazy), but deferring it keeps a test that injects
    its own runner fully independent of the spine.
    """
    from builder.agents.pipeline import run_pipeline

    return run_pipeline


def _default_guidance_runner() -> GuidanceRunner:
    """The real HITL guidance loop, imported lazily.

    Deferred so the import only happens on the interactive path — a headless
    build never imports guidance, and a test injecting its own runner is
    independent of the guidance module.
    """
    from builder.agents.guidance import run_guidance

    return run_guidance
