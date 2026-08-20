"""The live ReAct agent factory — one of the two architectures under test.

This wraps the as-built prose-prompt ReAct StateGraph (AGENTS.md §4 / D1) behind
the agent-agnostic :class:`~eval.agent_api.BuildAgent` contract. The
deterministic-pipeline factory (``pipeline_factory.py``, AGENTS.md §14) implements
the same contract; the harness A/B's the two by swapping factories.

A build:

1. creates a headless :class:`~builder.engine.AgentEngine` behind the production
   :class:`~builder.tools.hitl.SimulatedHumanInterface` — the SAME human the
   pipeline arm gets, so the A/B compares architectures and not environments
   (#609);
2. ``initialize(input_path)`` — scans the case's input dir if any, and (always)
   assigns a ``session_id`` + opens the run's ``profile.ndjson``;
3. runs the SHIPPED loop (``run_build(BuildMode.REACT, ..., interactive=False)``)
   with the case's prompt as its kickoff — the same budget users get, rather than
   the single bare graph invocation this used to make (#609);
4. returns the engine's final :class:`~builder.state.CrateState` and ``session_id``
   so the runner can mine that run's profile for tokens / latency / iterations.

**This calls a real LLM** — it is the harness's whole purpose when run live, and is
never exercised in CI. The model-driving step is injected (``graph_driver``) so the
wiring is unit-testable offline with the network stubbed out.
"""

from __future__ import annotations

import logging
from typing import Callable

from builder.engine import AgentEngine
from builder.tools.hitl import SimulatedHumanInterface
from eval.agent_api import BuildOutcome
from eval.corpus import EvalCase

logger = logging.getLogger(__name__)

# A graph_driver runs the ReAct loop for a prompt, mutating engine.state, and
# reports ``(stop_reason, error)`` — how the session ended, and why if it failed.
GraphDriver = Callable[[AgentEngine, str], tuple[str, str | None]]


def _live_graph_driver(
    engine: AgentEngine,
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> tuple[str, str | None]:
    """Drive the SHIPPED ReAct loop once for *prompt* (LIVE LLM call).

    Calls :func:`builder.agents.react.agent_loop.run_interactive_agent`, which is
    the arm users run. It used to build its own tools/model/graph here and
    ``invoke`` once — a copy of the loop's setup that measured a strictly smaller
    budget than the real thing: no wall-clock timeout guard, no self-continue,
    and no autonomous continuation, so a turn that narrated instead of asking
    ended the measured build then and there (#609).

    ``interactive=False`` tells the loop nobody is at the keyboard, so it skips
    its banner and greeting, runs *prompt* plus the autonomous continuation, and
    ends where it would otherwise read stdin — backstop and all.

    Returns:
        ``(stop_reason, error)`` — *error* is the last turn's failure reason, or
        ``None``. The loop absorbs model failures so the session survives them,
        so without that message the harness's transient-failure retry (which
        matches on the reason phrase) could never fire for this arm (#609).
    """
    from builder.agents.build import BuildMode, run_build

    result = run_build(
        BuildMode.REACT,
        engine,
        provider=provider,
        model=model,
        base_url=base_url,
        initial_prompt=prompt,
        interactive=False,
    )
    result = result or {}
    return str(result.get("stop_reason") or "completed"), result.get("error")


class ReActBuildAgent:
    """A :class:`~eval.agent_api.BuildAgent` backed by the live ReAct engine."""

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        graph_driver: GraphDriver | None = None,
    ) -> None:
        """Configure the agent's provider / model and (optional) driver override.

        Args:
            provider: LLM provider override (``"openai"`` / ``"anthropic"``); auto
                -detected from env when ``None``.
            model: Model name override.
            base_url: Custom OpenAI-compatible base URL.
            graph_driver: Injected driver (tests pass a stub). Defaults to the live
                LLM-calling :func:`_live_graph_driver`.
        """
        self._provider = provider
        self._model = model
        self._base_url = base_url
        self._graph_driver = graph_driver

    def _make_engine(self) -> AgentEngine:
        """Create a fresh headless engine — the same one the pipeline arm gets.

        Both arms use the production :class:`SimulatedHumanInterface`: nothing
        blocks on stdin, no scan root is widened, and the loop's
        RECOMMENDED/OPTIONAL validation escalation stays gated off, so neither arm
        pays for sweeps the other never runs (#609).
        """
        return AgentEngine(human_interface=SimulatedHumanInterface())

    def _driver(self) -> GraphDriver:
        """Return the configured driver, defaulting to the live LLM driver."""
        if self._graph_driver is not None:
            return self._graph_driver

        def driver(engine: AgentEngine, prompt: str) -> tuple[str, str | None]:
            return _live_graph_driver(
                engine,
                prompt,
                provider=self._provider,
                model=self._model,
                base_url=self._base_url,
            )

        return driver

    def build(self, case: EvalCase) -> BuildOutcome:
        """Build the crate for *case* by driving the ReAct loop once.

        Args:
            case: The corpus case to build.

        Returns:
            A :class:`BuildOutcome` with the final state, the run's ``session_id``,
            an ``error`` string if the driver raised, and a ``stop_reason``:
            ``"completed"`` (the loop self-terminated), ``"cap_hit"`` (it hit the
            recursion cap — trap 2, #331), or ``"error"``.
        """
        engine = self._make_engine()
        # initialize() scans the input dir (if any) and always assigns a
        # session_id + opens profile.ndjson — the source of this run's metrics.
        engine.initialize(input_path=case.input_path)

        error: str | None = None
        stop_reason = "completed"
        try:
            # The loop reports how it ended. A recursion cap is NOT an exception
            # here any more: the shipped loop catches it per-turn and keeps the
            # partial crate, so it comes back as ``"cap_hit"`` — a
            # valid-at-the-cutoff run, never a clean stop (trap 2, #331). Only a
            # driver that raises outright is an ``error``.
            stop_reason, error = self._driver()(engine, case.prompt)
        except Exception as exc:  # noqa: BLE001 — a failed build is a measured result
            logger.warning("ReAct build failed for case %s: %s", case.case_id, exc)
            error = str(exc)
            stop_reason = "error"
        finally:
            engine.close_profiler()

        return BuildOutcome(
            state=engine.state,
            session_id=engine.state.session_id,
            error=error,
            stop_reason=stop_reason,
        )


def make_react_agent_factory(
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> Callable[[], ReActBuildAgent]:
    """Return a zero-arg factory producing fresh :class:`ReActBuildAgent` instances.

    This is the callable handed to :func:`eval.runner.run_eval` for a live ReAct
    baseline. A future pipeline ships its own ``make_*_agent_factory`` with the
    same shape, and the harness compares them by swapping which factory it runs.
    """

    def factory() -> ReActBuildAgent:
        return ReActBuildAgent(provider=provider, model=model, base_url=base_url)

    return factory
