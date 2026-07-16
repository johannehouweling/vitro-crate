"""The live ReAct agent factory — one of the two architectures under test.

This wraps the as-built prose-prompt ReAct StateGraph (AGENTS.md §4 / D1) behind
the agent-agnostic :class:`~eval.agent_api.BuildAgent` contract. The
deterministic-pipeline factory (``pipeline_factory.py``, AGENTS.md §14) implements
the same contract; the harness A/B's the two by swapping factories.

A build:

1. creates a headless :class:`~builder.engine.AgentEngine`
   (:class:`~builder.tools.hitl.SimulatedHumanInterface`, so HITL auto-approves);
2. ``initialize(input_path)`` — scans the case's input dir if any, and (always)
   assigns a ``session_id`` + opens the run's ``profile.ndjson``;
3. drives the ReAct graph once with the case's prompt as a ``HumanMessage``;
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

# A graph_driver runs the ReAct loop once for a prompt, mutating engine.state.
GraphDriver = Callable[[AgentEngine, str], None]


def _live_graph_driver(
    engine: AgentEngine,
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> None:
    """Drive the existing LangGraph ReAct loop once for *prompt* (LIVE LLM call).

    Mirrors the non-interactive core of
    :func:`builder.agents.agent_loop.run_interactive_agent`: build the tools, the
    chat model, and the compiled graph (passing the engine so node timing lands in
    ``profile.ndjson``), then ``invoke`` it once with the prompt. The recursion
    limit is derived from the engine's configured iteration cap.
    """
    from typing import cast

    from langchain_core.messages import HumanMessage
    from langchain_core.runnables import RunnableConfig

    from builder.agents.agent_loop import (
        _build_agent_graph,
        _build_chat_model,
        _build_langchain_tools,
        _recursion_limit,
    )
    from builder.config import get_max_iterations

    tools = _build_langchain_tools(engine)
    llm = _build_chat_model(provider=provider, model=model, base_url=base_url)
    app = _build_agent_graph(llm, tools, engine=engine)

    max_iterations = get_max_iterations()
    config = cast(
        RunnableConfig,
        {
            "configurable": {"thread_id": engine.state.session_id},
            "recursion_limit": _recursion_limit(max_iterations),
        },
    )
    app.invoke({"messages": [HumanMessage(content=prompt)]}, config)


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
        """Create a fresh headless engine with a simulated human interface."""
        return AgentEngine(human_interface=SimulatedHumanInterface())

    def _driver(self) -> GraphDriver:
        """Return the configured driver, defaulting to the live LLM driver."""
        if self._graph_driver is not None:
            return self._graph_driver

        def driver(engine: AgentEngine, prompt: str) -> None:
            _live_graph_driver(
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
            and an ``error`` string if the driver raised.
        """
        engine = self._make_engine()
        # initialize() scans the input dir (if any) and always assigns a
        # session_id + opens profile.ndjson — the source of this run's metrics.
        engine.initialize(input_path=case.input_path)

        error: str | None = None
        try:
            self._driver()(engine, case.prompt)
        except Exception as exc:  # noqa: BLE001 — a failed build is a measured result
            logger.warning("ReAct build failed for case %s: %s", case.case_id, exc)
            error = str(exc)
        finally:
            engine.close_profiler()

        return BuildOutcome(
            state=engine.state,
            session_id=engine.state.session_id,
            error=error,
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
