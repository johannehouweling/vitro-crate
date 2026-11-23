"""The build's entrypoint: ``run_build`` hands the engine to the ReAct loop.

What the CLI and the eval know — the model selection, a resume, the opening
prompt, verbosity — reaches the loop unchanged, and the crate says which build
made it (#772).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from builder.agents.build import run_build

if TYPE_CHECKING:
    from builder.engine import AgentEngine


def _stub_engine() -> AgentEngine:
    """A dispatch-only engine double: just enough state for the build stamp (#772)."""
    from types import SimpleNamespace

    from builder.state import CrateState

    return cast("AgentEngine", SimpleNamespace(state=CrateState()))


class TestRunBuildDispatch:
    def test_react_dispatches_to_agent_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.react.agent_loop as agent_loop

        captured: dict[str, Any] = {}

        def _fake_agent(engine: Any, **kw: Any) -> str:
            captured.update(kw)
            return "return-value-is-ignored"

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _fake_agent)

        result = run_build(
            _stub_engine(),
            provider="openai",
            model="m",
            base_url="u",
        )

        # The kwargs are forwarded; the loop mutates state in place, so run_build
        # hands back whatever the loop reports — its ``{"stop_reason": ...}``,
        # which the eval harness records and the CLI ignores (#609).
        # `resumed` rides along on every dispatch and defaults to False — a build
        # that was not told it is a resume must not present itself as one (#410).
        # `initial_prompt` likewise defaults to None: no kickoff means the loop
        # keeps its conversational default and waits for a typed line (#412).
        assert captured == {
            "provider": "openai",
            "model": "m",
            "base_url": "u",
            "resumed": False,
            "initial_prompt": None,
        }
        assert result == "return-value-is-ignored"

    def test_resume_provenance_reaches_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--resume` is a caller fact and must survive the dispatch (#410)."""
        import builder.agents.react.agent_loop as agent_loop

        captured: dict[str, Any] = {}

        def _fake_agent(engine: Any, **kw: Any) -> None:
            captured.update(kw)

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _fake_agent)
        run_build(_stub_engine(), resumed=True)

        assert captured["resumed"] is True

    def test_initial_prompt_reaches_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--prompt` is the loop's kickoff and must survive the dispatch (#412)."""
        import builder.agents.react.agent_loop as agent_loop

        captured: dict[str, Any] = {}

        def _fake_agent(engine: Any, **kw: Any) -> None:
            captured.update(kw)

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _fake_agent)
        run_build(_stub_engine(), initial_prompt="build the crate")

        assert captured["initial_prompt"] == "build the crate"

    def test_verbose_reaches_the_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.react.agent_loop as agent_loop

        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            agent_loop, "run_interactive_agent", lambda engine, **kw: captured.update(kw)
        )

        run_build(_stub_engine(), verbose=True)

        assert captured["verbose"] is True


class TestGeneratorRecordsArchitecture:
    """The exported crate says which build made it (#772)."""

    @staticmethod
    def _run_properties(crate_dir: Path) -> dict[str, str]:
        """``name -> value`` of every PropertyValue on the crate's run action."""
        graph = json.loads((crate_dir / "ro-crate-metadata.json").read_text())["@graph"]
        by_id = {node["@id"]: node for node in graph}
        action = next(node for node in graph if node.get("@type") == "CreateAction")
        refs = action.get("additionalProperty") or []
        if isinstance(refs, dict):
            refs = [refs]
        return {by_id[r["@id"]]["name"]: by_id[r["@id"]]["value"] for r in refs}

    @staticmethod
    def _engine(out: Path) -> AgentEngine:
        from builder.engine import AgentEngine as _Engine
        from builder.state import CrateState

        engine = _Engine(state=CrateState())  # simulated human => headless path
        engine.initialize()
        engine.state.metadata.output_path = str(out)
        return engine

    def test_react_build_records_react(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.react.agent_loop as agent_loop
        from builder.tools.builder import export_crate

        monkeypatch.setattr(
            agent_loop, "run_interactive_agent", lambda engine, **kw: {"stop_reason": "done"}
        )
        out = tmp_path / "crate"
        engine = self._engine(out)

        run_build(engine)
        # The real ReAct loop exports from inside itself; the stub above cannot,
        # so drive the same export it would have run.
        export_crate(engine.state, str(out))

        assert self._run_properties(out)["Build architecture"] == "react"
