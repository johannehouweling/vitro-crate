"""The measured build meets the corpus behind the production headless human (#609).

The eval used to hand the ReAct factory an eval-only interface that reported
``is_interactive = True`` so a scan-root escalation could be approved (#329) —
but that flag is read by more than the scanner, and by the time it was removed
its stated purpose was already unreachable:

* a case WITH ``input_path`` never reaches the escalation at all — the engine's
  ``--input`` boundary refuses another directory before it consults the human;
* a case WITHOUT one (the prompt-only cases) reached it and **widened** the
  filesystem, which an unattended run never does;
* ``is_interactive`` is also the gate on the ReAct loop's RECOMMENDED/OPTIONAL
  validation escalation, so a measured build silently ran one or two extra full
  SHACL sweeps per passing build — paid for in tokens and wall-clock — that a
  headless run never runs.

These tests pin the headless behaviour, not the implementation.
"""

from __future__ import annotations

from pathlib import Path

from builder.engine import AgentEngine
from builder.tools.hitl import is_interactive
from eval.react_factory import make_react_agent_factory


def _engine() -> AgentEngine:
    return make_react_agent_factory()()._make_engine()


class TestTheHumanIsHeadless:
    def test_no_human_is_claimed_to_be_present(self) -> None:
        """``is_interactive`` means a real person can answer. Nobody can, here."""
        assert not is_interactive(_engine().human_interface)


class TestTheFilesystemIsNotWidened:
    def test_an_unapproved_directory_is_refused(self, tmp_path: Path) -> None:
        """The prompt-only corpus cases have no ``--input`` boundary to protect
        them, so the human is the only thing standing between the agent and any
        directory it names. It must refuse."""
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "secret.csv").write_text("x\n", encoding="utf-8")

        engine = _engine()
        engine.initialize()  # no input_path — the prompt-only case shape
        refusal = engine._authorize_scan_root(str(outside))
        assert refusal is not None, "the headless engine widened the filesystem"
        assert str(outside) not in engine.state.approved_scan_roots


class TestValidationIsNotEscalated:
    def test_the_recommended_optional_sweep_never_runs_headlessly(self, monkeypatch) -> None:
        """The ReAct loop offers RECOMMENDED/OPTIONAL sweeps once REQUIRED passes,
        gated on an interactive human. Headless, that gate must hold — an extra
        full SHACL sweep is real tokens and real wall-clock."""
        from builder.agents.react import agent_loop

        engine = _engine()
        calls: list[dict] = []

        def _record(tool_name: str, **kwargs: object) -> dict[str, bool]:
            calls.append({"name": tool_name, **kwargs})
            return {"ok": True}

        monkeypatch.setattr(engine, "run_tool", _record)
        agent_loop._run_validation_escalation(engine, {"ok": True})
        assert calls == [], f"escalated: {calls}"
