"""Tests for the deterministic-pipeline agent factory — offline (no LLM).

The pipeline factory implements the same :class:`~eval.agent_api.BuildAgent`
contract as the ReAct factory, so the harness A/B's the two by swapping factories.
Unlike ReAct, the pipeline is deterministic and calls no model when no provider is
configured, so these tests run it for real (the validator is offline against the
bundled context).
"""

from __future__ import annotations

from typing import cast

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

    def test_engine_uses_the_production_headless_human(self) -> None:
        """The same headless human the ReAct arm gets (#609)."""
        from builder.tools.hitl import SimulatedHumanInterface

        engine = make_pipeline_agent_factory()()._make_engine()
        assert type(engine.human_interface) is SimulatedHumanInterface

    def test_build_returns_conformant_outcome(self) -> None:
        """A real build of the minimal case reaches BASE + ISA conformance.

        TOX does not pass here, and that is correct rather than a regression: the
        minimal case runs the spine with no provider, so nothing states an
        exposure duration or detection instrument, and ``_pv`` will not publish
        "unknown" as though it were a measurement (D5). The remaining issues must
        be exactly that gap — any OTHER issue is a real wiring regression, which
        is what this test is here to catch.
        """
        from eval.corpus import reaches_isa_tox_conformance

        agent = PipelineBuildAgent()
        minimal = next(c for c in DEFAULT_CORPUS if c.kind == "minimal")
        outcome = agent.build(minimal)

        assert isinstance(outcome, BuildOutcome)
        assert isinstance(outcome.state, CrateState)
        assert outcome.session_id  # initialize() assigns one
        assert outcome.error is None

        verdict = reaches_isa_tox_conformance(outcome.state)
        assert verdict["conformance"]["base"] is True
        assert verdict["conformance"]["isa"] is True
        assert all(
            str(issue.get("property", "")).endswith("additionalProperty")
            for issue in verdict["issues"]
        ), verdict["issues"]

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


class TestPipelineTokenAccounting:
    """Issue #221 — the eval case record must sum the deterministic spine's leaf
    LLM token usage, the SAME way the ReAct arm does (mined from profile.ndjson).
    Previously the pipeline arm recorded 0 because the leaves' usage was discarded.

    Fully offline: a fake leaf reports a known usage payload through the
    ``usage_sink`` the spine passes it; the real ``run_pipeline`` logs those as
    ``node_end``/``node="model"`` profile events, and ``run_eval`` mines them with
    its default (disk-backed) profile reader.
    """

    def test_eval_record_sums_pipeline_leaf_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.pipeline.pipeline as pipeline_mod
        from eval.runner import run_eval

        # Provider "configured" + a fake leaf that emits a known usage payload on
        # every call. No real model, no network.
        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: "openai")
        monkeypatch.setattr(pipeline_mod, "extract_plan", lambda *a, **k: {})

        calls = {"n": 0}

        def fake_leaf(entity_type, context, *, overrides=None, usage_sink=None):
            calls["n"] += 1
            if usage_sink is not None:
                usage_sink(100, 25, "gpt-4o-mini")
            return {"description": f"drafted {entity_type}"}

        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", fake_leaf)

        # A custom agent: real engine (so a real profile.ndjson is written), seed
        # a title so the drafter has usable context, then run the REAL spine.
        class _SpineAgent:
            def __init__(self) -> None:
                self._engine = AgentEngine(human_interface=SimulatedHumanInterface())

            def build(self, case):  # type: ignore[no-untyped-def]
                self._engine.initialize(input_path=case.input_path)
                # Give the drafter usable context so it makes leaf calls.
                self._engine.state.metadata.title = "Token accounting probe"
                error = None
                try:
                    pipeline_mod.run_pipeline(self._engine)
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
                finally:
                    self._engine.close_profiler()
                return BuildOutcome(
                    state=self._engine.state,
                    session_id=self._engine.state.session_id,
                    error=error,
                )

        minimal = next(c for c in DEFAULT_CORPUS if c.kind == "minimal")
        report = run_eval(lambda: _SpineAgent(), [minimal], repeats=1, label="pipe-tok")

        res = report.results[0]
        assert calls["n"] >= 1, "the spine must have made >=1 leaf call"
        # The eval record sums every leaf call (calls × 100/25) — non-zero, in the
        # SAME fields the ReAct arm uses.
        assert res.input_tokens == 100 * calls["n"]
        assert res.output_tokens == 25 * calls["n"]
        assert res.total_tokens == 125 * calls["n"]

    def test_no_provider_records_clean_zero_through_eval(self) -> None:
        """The no-provider pipeline path records a clean 0 — no crash, no leak."""
        from eval.runner import run_eval

        # The real PipelineBuildAgent with NO provider configured: the spine's
        # drafter/plan steps are strict no-ops, so the record is a clean zero.
        factory = make_pipeline_agent_factory()
        minimal = next(c for c in DEFAULT_CORPUS if c.kind == "minimal")
        report = run_eval(factory, [minimal], repeats=1, label="pipe-zero")

        res = report.results[0]
        assert res.input_tokens == 0
        assert res.output_tokens == 0
        assert res.total_tokens == 0


class TestPipelineAgentModelOverrides:
    """The A/B's model selection must reach this arm too (#399).

    `--model X` was threaded into the ReAct factory and dropped here, while the
    pipeline's bounded leaves still resolved a model from the ENVIRONMENT. So a
    run asked to compare two architectures on one model silently compared two
    models, and part of any token or cost delta was a model delta.
    """

    def test_overrides_reach_the_spine(self) -> None:
        from builder.agents.llm import ModelOverrides

        seen: dict = {}

        def _runner(engine, **kwargs):
            seen.update(kwargs)
            return {}

        pinned = ModelOverrides(provider="openai", model="gpt-5.6-luna")
        agent = PipelineBuildAgent(pipeline_runner=_runner, overrides=pinned)
        agent.build(DEFAULT_CORPUS[0])

        assert seen.get("overrides") == pinned

    def test_factory_forwards_what_the_cli_selected(self) -> None:
        """`select_agent_factory` must hand the CLI's choice to this arm."""
        from builder.agents.build import BuildMode
        from builder.agents.llm import ModelOverrides
        from eval.__main__ import select_agent_factory

        factory = select_agent_factory(
            BuildMode.PIPELINE, provider="openai", model="gpt-5.6-luna", base_url=None
        )
        agent = cast(PipelineBuildAgent, factory())

        assert agent._overrides == ModelOverrides(
            provider="openai", model="gpt-5.6-luna", base_url=None
        )

    def test_a_narrow_injected_runner_is_still_callable(self) -> None:
        """HONESTY CONTROL — threading must not break a stub that predates it.

        A runner whose signature is `(engine)` only must still be called the
        legacy way rather than raising an unexpected-keyword TypeError, which the
        build would swallow into a silent `stop_reason="error"`.
        """
        from builder.agents.llm import ModelOverrides

        calls: list = []

        def _narrow_runner(engine):
            calls.append(engine)
            return {}

        agent = PipelineBuildAgent(
            pipeline_runner=_narrow_runner, overrides=ModelOverrides(model="x")
        )
        outcome = agent.build(DEFAULT_CORPUS[0])

        assert len(calls) == 1
        assert outcome.error is None
        assert outcome.stop_reason == "completed"
