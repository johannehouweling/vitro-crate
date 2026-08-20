"""Tests for the live ReAct agent factory — offline (no LLM, no network).

The factory wires a headless :class:`~builder.engine.AgentEngine` (with a
:class:`~builder.tools.hitl.SimulatedHumanInterface`) and drives the existing
LangGraph ReAct loop from a case's prompt. We never invoke a real model here: the
graph-driving call is injected so the test can stub it, leaving only the wiring —
engine creation, headless interface, input init, and outcome shaping — under test.
"""

from __future__ import annotations

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.hitl import SimulatedHumanInterface
from eval.agent_api import BuildAgent, BuildOutcome
from eval.corpus import DEFAULT_CORPUS
from eval.react_factory import ReActBuildAgent, make_react_agent_factory


class TestReActAgentWiring:
    def test_factory_returns_a_build_agent(self) -> None:
        factory = make_react_agent_factory()
        agent = factory()
        assert isinstance(agent, BuildAgent)

    def test_engine_uses_the_production_headless_human(self) -> None:
        """Both arms get the SAME headless human, so the A/B compares
        architectures and not environments (#609); see
        ``eval/tests/test_arm_symmetry.py`` for what that symmetry buys."""
        from builder.tools.hitl import SimulatedHumanInterface

        engine = make_react_agent_factory()()._make_engine()
        assert type(engine.human_interface) is SimulatedHumanInterface

    def test_build_returns_outcome_with_state_and_session_id(self) -> None:
        # Inject a fake graph-driver so no model is contacted: it just records the
        # prompt and leaves the engine's state in place.
        seen: dict[str, str] = {}

        def fake_driver(engine: AgentEngine, prompt: str) -> None:
            seen["prompt"] = prompt

        agent = ReActBuildAgent(graph_driver=fake_driver)
        case = DEFAULT_CORPUS[0]
        outcome = agent.build(case)

        assert isinstance(outcome, BuildOutcome)
        assert isinstance(outcome.state, CrateState)
        assert outcome.session_id  # initialize() sets a session id
        assert seen["prompt"] == case.prompt

    def test_structured_case_initializes_from_its_input_path(self) -> None:
        captured: dict[str, object] = {}

        def fake_driver(engine: AgentEngine, prompt: str) -> None:
            captured["input_path"] = engine.state.metadata.input_path

        agent = ReActBuildAgent(graph_driver=fake_driver)
        structured = next(c for c in DEFAULT_CORPUS if c.kind == "structured")
        agent.build(structured)
        assert captured["input_path"] == structured.input_path

    def test_build_captures_a_driver_error_as_outcome_error(self) -> None:
        def boom(engine: AgentEngine, prompt: str) -> None:
            raise RuntimeError("model unreachable")

        agent = ReActBuildAgent(graph_driver=boom)
        outcome = agent.build(DEFAULT_CORPUS[0])
        assert outcome.error is not None
        assert "model unreachable" in outcome.error
        # Even on failure we still return the (partial) state for inspection.
        assert isinstance(outcome.state, CrateState)

    def test_minimal_case_has_no_input_path(self) -> None:
        captured: dict[str, object] = {}

        def fake_driver(engine: AgentEngine, prompt: str) -> None:
            captured["input_path"] = engine.state.metadata.input_path

        agent = ReActBuildAgent(graph_driver=lambda e, p: captured.__setitem__(
            "input_path", e.state.metadata.input_path
        ))
        minimal = next(c for c in DEFAULT_CORPUS if c.kind == "minimal")
        agent.build(minimal)
        assert captured["input_path"] is None
