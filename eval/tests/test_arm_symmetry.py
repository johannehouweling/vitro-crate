"""Both arms meet the corpus behind the SAME headless human (#609).

The A/B is only an architecture comparison if the two arms are handed the same
environment. The eval used to give both arms an eval-only interface that reported
``is_interactive = True`` so a scan-root escalation could be approved (#329) —
but that flag is read by more than the scanner, and by the time it was removed
its stated purpose was already unreachable:

* a case WITH ``input_path`` never reaches the escalation at all — the engine's
  ``--input`` boundary refuses another directory before it consults the human;
* a case WITHOUT one (the prompt-only cases) reached it and **widened** the
  filesystem for the ReAct arm alone, which the pipeline arm never does;
* ``is_interactive`` is also the gate on the ReAct loop's RECOMMENDED/OPTIONAL
  validation escalation, so the ReAct arm silently ran one or two extra full
  SHACL sweeps per passing build — paid for in tokens and wall-clock — that the
  pipeline arm never runs.

These tests pin the symmetry, not the implementation.
"""

from __future__ import annotations

from pathlib import Path

from builder.engine import AgentEngine
from builder.tools.hitl import SimulatedHumanInterface, is_interactive
from eval.pipeline_factory import make_pipeline_agent_factory
from eval.react_factory import make_react_agent_factory


def _engines() -> dict[str, AgentEngine]:
    return {
        "react": make_react_agent_factory()()._make_engine(),
        "pipeline": make_pipeline_agent_factory()()._make_engine(),
    }


class TestBothArmsGetTheSameHuman:
    def test_neither_arm_claims_a_human_is_present(self) -> None:
        """``is_interactive`` means a real person can answer. Nobody can, here."""
        for arm, engine in _engines().items():
            assert not is_interactive(engine.human_interface), arm

    def test_both_arms_use_the_production_headless_interface(self) -> None:
        """No eval-only subclass: the arms meet the corpus as batch mode does."""
        for arm, engine in _engines().items():
            assert type(engine.human_interface) is SimulatedHumanInterface, arm


class TestNeitherArmCanWidenTheFilesystem:
    def test_an_unapproved_directory_is_refused_on_both_arms(self, tmp_path: Path) -> None:
        """The prompt-only corpus cases have no ``--input`` boundary to protect
        them, so the human is the only thing standing between the agent and any
        directory it names. It must refuse on both arms."""
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "secret.csv").write_text("x\n", encoding="utf-8")

        for arm, engine in _engines().items():
            engine.initialize()  # no input_path — the prompt-only case shape
            refusal = engine._authorize_scan_root(str(outside))
            assert refusal is not None, f"{arm} widened the filesystem"
            assert str(outside) not in engine.state.approved_scan_roots, arm


class TestNeitherArmEscalatesValidation:
    def test_the_recommended_optional_sweep_never_runs_headlessly(self, monkeypatch) -> None:
        """The ReAct loop offers RECOMMENDED/OPTIONAL sweeps once REQUIRED passes,
        gated on an interactive human. Headless, that gate must hold on both arms —
        an extra full SHACL sweep is real tokens and real wall-clock on one arm only.
        """
        from builder.agents.react import agent_loop

        for arm, engine in _engines().items():
            calls: list[dict] = []

            def _record(tool_name: str, **kwargs: object) -> dict[str, bool]:
                calls.append({"name": tool_name, **kwargs})
                return {"ok": True}

            monkeypatch.setattr(engine, "run_tool", _record)
            agent_loop._run_validation_escalation(engine, {"ok": True})
            assert calls == [], f"{arm} escalated: {calls}"


class TestBothArmsAreScoredAtTheSameStage:
    """Export is not a formality — it MUTATES the crate.

    ``export_crate`` runs an optional-tier ``ensure_validated`` and
    ``wire_unreferenced_domain_entities`` (its only caller), which ``set_fields``
    on loose MolecularEntity / CellLineSample entities. The ReAct arm exports
    from inside its loop (``_auto_export_after_build``), so it was scored on a
    wired, optional-swept crate while the pipeline arm was scored on the state
    the spine left behind — a difference in the measurement, not in the
    architectures (#609).
    """

    def test_the_pipeline_arm_exports_like_the_react_arm(self, tmp_path: Path) -> None:
        from eval.corpus import EvalCase

        deposit = tmp_path / "deposit"
        deposit.mkdir()
        (deposit / "readme.txt").write_text("an in vitro assay\n", encoding="utf-8")

        def _spine(engine, **kwargs):
            engine.state.metadata.title = "Exported study"
            return {"conformance": {}}

        agent = make_pipeline_agent_factory()()
        agent._pipeline_runner = _spine
        case = EvalCase(
            case_id="export-probe",
            description="",
            kind="structured",
            prompt="build it",
            input_path=str(deposit),
        )

        outcome = agent.build(case)

        assert outcome.state.metadata.exported_at, (
            "the pipeline arm was scored on a crate that was never exported, "
            "while the ReAct arm's crate was"
        )


class TestAProblemNeitherArmCanShareIsNotAWin:
    """Two corpus cases carry a prompt and no input directory. The pipeline is
    folder-driven **by design** — ``main.py`` refuses ``--interactive`` with no
    input and points the user at ``--react`` — so it never read ``case.prompt``,
    scaffolded an empty crate, passed conformance and was scored a win at $0
    while the ReAct arm drafted the whole study from the brief. That is the
    cheapest possible way to look cheap (#609).
    """

    def test_a_prompt_only_case_is_not_applicable_to_the_pipeline(self) -> None:
        from eval.corpus import EvalCase

        case = EvalCase(
            case_id="prompt-only", description="", kind="unstructured", prompt="build it"
        )
        outcome = make_pipeline_agent_factory()().build(case)

        assert outcome.stop_reason == "not_applicable"

    def test_a_not_applicable_case_is_left_out_of_the_arm_average(self) -> None:
        """Averaging in a case one arm cannot attempt reports a capability gap as
        a cost win. It is counted and named instead."""
        from eval.runner import CaseResult, EvalReport

        run = EvalReport(
            label="pipeline",
            repeats=1,
            results=[
                CaseResult(
                    case_id="real", kind="structured", success=True, conformance={},
                    issues=[], input_tokens=1000, output_tokens=500, tool_calls=4,
                    iterations=2, latency_seconds=10.0, crate_hashes=["a"],
                    deterministic=None, repeats=1, stop_reason="completed",
                    total_tokens_per_repeat=[1500],
                ),
                CaseResult(
                    case_id="prompt-only", kind="unstructured", success=False,
                    conformance={}, issues=[], input_tokens=0, output_tokens=0,
                    tool_calls=0, iterations=0, latency_seconds=0.0, crate_hashes=[],
                    deterministic=None, repeats=1, stop_reason="not_applicable",
                    total_tokens_per_repeat=[0],
                ),
            ],
        )
        summary = run.summary()

        assert summary["num_not_applicable"] == 1
        assert summary["num_cases_compared"] == 1
        assert summary["success_rate"] == 1.0, "the skipped case dragged the average"
        assert summary["mean_total_tokens"] == 1500.0
