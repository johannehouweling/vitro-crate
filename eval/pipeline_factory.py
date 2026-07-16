"""The deterministic-pipeline agent factory — the §14 architecture under test.

This wraps the deterministic pipeline spine (:func:`builder.agents.pipeline.pipeline.run_pipeline`,
AGENTS.md §14.2) behind the agent-agnostic :class:`~eval.agent_api.BuildAgent`
contract — the *same* contract the live ReAct factory (:mod:`eval.react_factory`)
implements. The harness then A/B's the two architectures by swapping which factory
it runs (``--arch react|pipeline`` in :mod:`eval.__main__`); the corpus, metrics,
and report are unchanged.

A build:

1. creates a headless :class:`~builder.engine.AgentEngine`
   (:class:`~builder.tools.hitl.SimulatedHumanInterface`, so HITL auto-denies);
2. ``initialize(input_path)`` — scans the case's input dir if any (which approves
   that dir under the #198 fail-closed guard), and assigns a ``session_id`` +
   opens the run's ``profile.ndjson``;
3. runs the deterministic spine once over the engine — **no LLM, no network**;
4. returns the engine's final :class:`~builder.state.CrateState` and ``session_id``,
   exactly like :class:`~eval.react_factory.ReActBuildAgent`.

Unlike ReAct this calls **no model**, so it runs in CI for real. The spine call is
injected (``pipeline_runner``) so the wiring is unit-testable in isolation.
"""

from __future__ import annotations

import logging
from typing import Callable

from builder.engine import AgentEngine
from builder.tools.hitl import SimulatedHumanInterface
from eval.agent_api import BuildOutcome
from eval.corpus import EvalCase

logger = logging.getLogger(__name__)

# A pipeline_runner runs the deterministic spine once over an engine, mutating
# engine.state and returning the spine's result dict.
PipelineRunner = Callable[[AgentEngine], dict]


class PipelineBuildAgent:
    """A :class:`~eval.agent_api.BuildAgent` backed by the deterministic spine."""

    def __init__(self, *, pipeline_runner: PipelineRunner | None = None) -> None:
        """Configure the agent's spine runner.

        Args:
            pipeline_runner: Injected runner (tests pass a stub). Defaults to the
                real :func:`builder.agents.pipeline.pipeline.run_pipeline`.
        """
        self._pipeline_runner = pipeline_runner

    def _make_engine(self) -> AgentEngine:
        """Create a fresh headless engine with a simulated human interface."""
        return AgentEngine(human_interface=SimulatedHumanInterface())

    def _runner(self) -> PipelineRunner:
        """Return the configured runner, defaulting to the real spine."""
        if self._pipeline_runner is not None:
            return self._pipeline_runner

        from builder.agents.pipeline.pipeline import run_pipeline

        return run_pipeline

    def build(self, case: EvalCase) -> BuildOutcome:
        """Build the crate for *case* by running the deterministic spine once.

        Args:
            case: The corpus case to build.

        Returns:
            A :class:`BuildOutcome` with the final state, the run's ``session_id``,
            and an ``error`` string if the spine raised.
        """
        engine = self._make_engine()
        # initialize() scans the input dir (if any) — which approves it under the
        # fail-closed guard — and always assigns a session_id + opens
        # profile.ndjson, the source of this run's metrics.
        engine.initialize(input_path=case.input_path)

        error: str | None = None
        try:
            self._runner()(engine)
        except Exception as exc:  # noqa: BLE001 — a failed build is a measured result
            logger.warning("Pipeline build failed for case %s: %s", case.case_id, exc)
            error = str(exc)
        finally:
            engine.close_profiler()

        return BuildOutcome(
            state=engine.state,
            session_id=engine.state.session_id,
            error=error,
        )


def make_pipeline_agent_factory() -> Callable[[], PipelineBuildAgent]:
    """Return a zero-arg factory producing fresh :class:`PipelineBuildAgent` instances.

    This is the callable handed to :func:`eval.runner.run_eval` for a deterministic
    pipeline run. It mirrors :func:`eval.react_factory.make_react_agent_factory` so
    the harness compares them by swapping which factory it runs.
    """

    def factory() -> PipelineBuildAgent:
        return PipelineBuildAgent()

    return factory
