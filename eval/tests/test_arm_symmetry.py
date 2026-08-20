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
