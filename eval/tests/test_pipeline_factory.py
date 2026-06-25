"""Tests for the deterministic-pipeline agent factory — offline (no LLM).

The pipeline factory implements the same :class:`~eval.agent_api.BuildAgent`
contract as the ReAct factory, so the harness A/B's the two by swapping factories.
Unlike ReAct, the pipeline is fully deterministic and calls no model, so these
tests run it for real (the validator is offline against the bundled context).
"""

from __future__ import annotations

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.hitl import SimulatedHumanInterface
from eval.agent_api import BuildAgent, BuildOutcome
from eval.corpus import DEFAULT_CORPUS
from eval.pipeline_factory import PipelineBuildAgent, make_pipeline_agent_factory

# The pipeline drives the SHACL validator; give the module headroom over the CLI
# default like the other validation-heavy modules.
pytestmark = pytest.mark.timeout(120)


class TestPipelineAgentWiring:
    def test_factory_returns_a_build_agent(self) -> None:
        factory = make_pipeline_agent_factory()
        agent = factory()
        assert isinstance(agent, BuildAgent)

    def test_engine_uses_a_headless_human_interface(self) -> None:
        agent = PipelineBuildAgent()
        engine = agent._make_engine()
        assert isinstance(engine, AgentEngine)
        assert isinstance(engine.human_interface, SimulatedHumanInterface)

    def test_build_returns_conformant_outcome(self) -> None:
        """A real build of the minimal case reaches {base,isa,tox} conformance."""
        from eval.corpus import reaches_isa_tox_conformance

        agent = PipelineBuildAgent()
        minimal = next(c for c in DEFAULT_CORPUS if c.kind == "minimal")
        outcome = agent.build(minimal)

        assert isinstance(outcome, BuildOutcome)
        assert isinstance(outcome.state, CrateState)
        assert outcome.session_id  # initialize() assigns one
        assert outcome.error is None
        assert reaches_isa_tox_conformance(outcome.state)["success"] is True

    def test_pipeline_runner_is_injectable(self) -> None:
        """The pipeline-driver is injected so wiring is unit-testable in isolation."""
        seen: dict[str, object] = {}

        def fake_pipeline(engine: AgentEngine) -> dict:
            seen["called"] = True
            seen["state"] = engine.state
            return {"ok": True, "conformance": {}}

        agent = PipelineBuildAgent(pipeline_runner=fake_pipeline)
        outcome = agent.build(DEFAULT_CORPUS[0])
        assert seen.get("called") is True
        assert outcome.state is seen["state"]

    def test_structured_case_initializes_from_its_input_path(self) -> None:
        captured: dict[str, object] = {}

        def fake_pipeline(engine: AgentEngine) -> dict:
            captured["input_path"] = engine.state.metadata.input_path
            return {"ok": True, "conformance": {}}

        agent = PipelineBuildAgent(pipeline_runner=fake_pipeline)
        structured = next(c for c in DEFAULT_CORPUS if c.kind == "structured")
        agent.build(structured)
        assert captured["input_path"] == structured.input_path

    def test_build_captures_a_runner_error_as_outcome_error(self) -> None:
        def boom(engine: AgentEngine) -> dict:
            raise RuntimeError("pipeline exploded")

        agent = PipelineBuildAgent(pipeline_runner=boom)
        outcome = agent.build(DEFAULT_CORPUS[0])
        assert outcome.error is not None
        assert "pipeline exploded" in outcome.error
        assert isinstance(outcome.state, CrateState)
