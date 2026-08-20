"""The deterministic-pipeline agent factory — the §14 architecture under test.

This wraps the deterministic pipeline spine (:func:`builder.agents.pipeline.pipeline.run_pipeline`,
AGENTS.md §14.2) behind the agent-agnostic :class:`~eval.agent_api.BuildAgent`
contract — the *same* contract the live ReAct factory (:mod:`eval.react_factory`)
implements. The harness then A/B's the two architectures by swapping which factory
it runs (``--arch react|pipeline`` in :mod:`eval.__main__`); the corpus, metrics,
and report are unchanged.

A build:

1. creates a headless :class:`~builder.engine.AgentEngine` behind the production
   :class:`~builder.tools.hitl.SimulatedHumanInterface` — the SAME human the ReAct
   arm gets, so the A/B compares architectures and not environments (#609);
2. ``initialize(input_path)`` — scans the case's input dir if any (which approves
   that dir under the #198 fail-closed guard), and assigns a ``session_id`` +
   opens the run's ``profile.ndjson``;
3. runs the deterministic spine once over the engine — **no LLM, no network**;
4. returns the engine's final :class:`~builder.state.CrateState` and ``session_id``,
   exactly like :class:`~eval.react_factory.ReActBuildAgent`.

This arm DOES call a model — the spine's bounded leaves run on the drafter tier —
but only when a provider is configured, so with none set it runs in CI for real as
a strict no-op. The caller's provider/model/base URL is threaded to those leaves
(#399); it used to be dropped here while ReAct received it, so a "same-model" A/B
compared two different models. The spine call is injected (``pipeline_runner``) so
the wiring is unit-testable in isolation.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from builder.agents.llm import ModelOverrides
from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.hitl import SimulatedHumanInterface
from eval.agent_api import BuildOutcome
from eval.corpus import EvalCase

logger = logging.getLogger(__name__)


# A pipeline_runner runs the deterministic spine once over an engine, mutating
# engine.state and returning the spine's result dict.
# The open ``(...)`` form mirrors ``builder.agents.build.PipelineRunner``: the real
# ``run_pipeline`` takes keyword-only extras (``overrides``, #399) that a narrow
# injected stub may not, so the call site introspects rather than assuming.
PipelineRunner = Callable[..., dict]


class PipelineBuildAgent:
    """A :class:`~eval.agent_api.BuildAgent` backed by the deterministic spine."""

    def __init__(
        self,
        *,
        pipeline_runner: PipelineRunner | None = None,
        overrides: ModelOverrides | None = None,
    ) -> None:
        """Configure the agent's spine runner.

        Args:
            pipeline_runner: Injected runner (tests pass a stub). Defaults to the
                real :func:`builder.agents.pipeline.pipeline.run_pipeline`.
            overrides: Caller-pinned provider/model/base URL for the spine's
                bounded leaves (#399). ``None`` resolves from the environment.
        """
        self._pipeline_runner = pipeline_runner
        self._overrides = overrides

    def _make_engine(self) -> AgentEngine:
        """Create a fresh headless engine — the same one the ReAct arm gets.

        Both arms use the production :class:`SimulatedHumanInterface` so the two
        are wired identically (#609).
        """
        return AgentEngine(human_interface=SimulatedHumanInterface())

    def build(self, case: EvalCase) -> BuildOutcome:
        """Build the crate for *case* by running the deterministic spine once.

        Args:
            case: The corpus case to build.

        Returns:
            A :class:`BuildOutcome` with the final state, the run's ``session_id``,
            an ``error`` string if the spine raised, and a ``stop_reason``. The
            deterministic spine always self-terminates, so this is ``"completed"``
            on success and ``"error"`` when it raises — it never ``"cap_hit"``
            (there is no recursion loop to cap).
        """
        if case.input_path is None:
            # A conversational case: a prompt and no documents. This arm is
            # folder-driven BY DESIGN — `main.py --interactive` refuses to run it
            # with nothing scanned and points the user at `--react` — so it has
            # no way to attempt this and never read `case.prompt`. Left to run it
            # scaffolded an empty crate, passed conformance and scored a win at
            # $0 against an arm that drafted the whole study from the brief.
            # Say so instead (#609).
            logger.info(
                "Case %s is conversational (no input directory); the deterministic "
                "pipeline does not attempt it",
                case.case_id,
            )
            return BuildOutcome(state=CrateState(), stop_reason="not_applicable")

        engine = self._make_engine()
        # initialize() scans the input dir (if any) — which approves it under the
        # fail-closed guard — and always assigns a session_id + opens
        # profile.ndjson, the source of this run's metrics.
        engine.initialize(input_path=case.input_path)

        error: str | None = None
        stop_reason = "completed"
        report: dict[str, Any] | None = None
        try:
            # The SHIPPED pipeline entrypoint, not the bare spine: it runs the
            # spine, then exports and persists. Export is not a formality — it
            # runs an optional-tier `ensure_validated` and
            # `wire_unreferenced_domain_entities`, both of which change the crate.
            # The ReAct arm exports from inside its own loop, so scoring the
            # pipeline on the state the spine left behind compared two different
            # stages of two builds (#609). The human is headless, so the guidance
            # tail is correctly skipped and this stays an automated-vs-automated
            # comparison.
            from builder.agents.build import run_interactive_build

            raw = run_interactive_build(
                engine,
                pipeline_runner=self._pipeline_runner,
                overrides=self._overrides,
            )
            # run_pipeline's structured report (conformance, issues, the
            # materialization outcomes incl. condition_table, data_issues) used
            # to be discarded here, hiding every domain-level failure that does
            # not raise (#422). Keep it whenever the runner returns one.
            report = raw.get("pipeline") if isinstance(raw, dict) else None
        except Exception as exc:  # noqa: BLE001 — a failed build is a measured result
            logger.warning("Pipeline build failed for case %s: %s", case.case_id, exc)
            error = str(exc)
            stop_reason = "error"
        finally:
            engine.close_profiler()

        return BuildOutcome(
            state=engine.state,
            session_id=engine.state.session_id,
            error=error,
            stop_reason=stop_reason,
            pipeline_result=report,
        )


def make_pipeline_agent_factory(
    *, overrides: ModelOverrides | None = None
) -> Callable[[], PipelineBuildAgent]:
    """Return a zero-arg factory producing fresh :class:`PipelineBuildAgent` instances.

    This is the callable handed to :func:`eval.runner.run_eval` for a deterministic
    pipeline run. It mirrors :func:`eval.react_factory.make_react_agent_factory` so
    the harness compares them by swapping which factory it runs — including in
    taking the caller's model selection, which this arm used to ignore (#399).
    """

    def factory() -> PipelineBuildAgent:
        return PipelineBuildAgent(overrides=overrides)

    return factory
