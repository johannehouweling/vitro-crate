"""The ``BuildMode`` switch — the single A/B mode selector (Issue #309, Step 3).

Both the CLI (``main.py``) and the eval harness flip between the deterministic
pipeline and the ReAct loop through *one* enum + dispatch in
``builder.agents.build``, instead of each hard-wiring its own boolean/string. The
two modes drive the same engine + toolbox; only orchestration differs (AGENTS.md
§1, D15 — both stay first-class).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from builder.agents.build import BuildMode, run_build

if TYPE_CHECKING:
    from builder.engine import AgentEngine


def _stub_engine() -> AgentEngine:
    """A dispatch-only engine double: just enough state for the arm stamp (#772)."""
    from types import SimpleNamespace

    from builder.state import CrateState

    return cast("AgentEngine", SimpleNamespace(state=CrateState()))


class TestBuildModeFromCli:
    def test_react_maps_to_react(self) -> None:
        assert BuildMode.from_cli(react=True) is BuildMode.REACT

    def test_default_maps_to_pipeline(self) -> None:
        assert BuildMode.from_cli(react=False) is BuildMode.PIPELINE

    def test_values_match_eval_arch_strings(self) -> None:
        """The eval's ``--arch`` choices and the BuildMode enum values MUST stay in
        lockstep so ``BuildMode(args.arch)`` always resolves. Assert against the REAL
        parser choices — not inline literals — so a change to either side that breaks
        the mapping (a renamed/added arch, a dropped enum value) fails here.
        """
        from eval.__main__ import build_arg_parser

        parser = build_arg_parser()
        arch = next(a for a in parser._actions if "--arch" in a.option_strings)
        # argparse types `choices` as optional; a `--arch` with no choices at all
        # would silently pass both assertions below, so pin it here.
        choices = arch.choices
        assert choices is not None, "--arch must constrain its choices"
        assert set(choices) == {m.value for m in BuildMode}
        # Every CLI choice round-trips through the shared enum.
        for choice in choices:
            assert BuildMode(choice).value == choice


class TestRunBuildDispatch:
    def test_pipeline_dispatches_to_interactive_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.build as build_mod

        captured: dict[str, Any] = {}

        def _fake_build(engine: Any, **kw: Any) -> dict[str, Any]:
            captured["kw"] = kw
            return {"pipeline": {}, "guidance": None}

        monkeypatch.setattr(build_mod, "run_interactive_build", _fake_build)

        result = run_build(BuildMode.PIPELINE, _stub_engine(), output=print)

        assert captured["kw"].get("output") is print
        assert result == {"pipeline": {}, "guidance": None}

    def test_react_dispatches_to_agent_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.react.agent_loop as agent_loop

        captured: dict[str, Any] = {}

        def _fake_agent(engine: Any, **kw: Any) -> str:
            captured.update(kw)
            return "return-value-is-ignored"

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _fake_agent)

        result = run_build(
            BuildMode.REACT,
            _stub_engine(),
            provider="openai",
            model="m",
            base_url="u",
        )

        # ReAct-only kwargs are forwarded; the loop mutates state in place, so
        # run_build hands back whatever the loop reports — its
        # ``{"stop_reason": ...}``, which the A/B harness records and the CLI
        # ignores (#609) — rather than a structured pipeline result.
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

    def test_resume_provenance_reaches_the_react_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--resume` is a caller fact and must survive the dispatch (#410)."""
        import builder.agents.react.agent_loop as agent_loop

        captured: dict[str, Any] = {}

        def _fake_agent(engine: Any, **kw: Any) -> None:
            captured.update(kw)

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _fake_agent)
        run_build(BuildMode.REACT, _stub_engine(), resumed=True)

        assert captured["resumed"] is True

    def test_initial_prompt_reaches_the_react_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--prompt` is the ReAct kickoff and must survive the dispatch (#412)."""
        import builder.agents.react.agent_loop as agent_loop

        captured: dict[str, Any] = {}

        def _fake_agent(engine: Any, **kw: Any) -> None:
            captured.update(kw)

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _fake_agent)
        run_build(BuildMode.REACT, _stub_engine(), initial_prompt="build the crate")

        assert captured["initial_prompt"] == "build the crate"

    def test_verbose_reaches_the_react_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.react.agent_loop as agent_loop

        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            agent_loop, "run_interactive_agent", lambda engine, **kw: captured.update(kw)
        )

        run_build(BuildMode.REACT, _stub_engine(), verbose=True)

        assert captured["verbose"] is True

    def test_initial_prompt_is_not_forwarded_to_the_pipeline_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pipeline auto-runs and takes no opening message — mode-specific kwarg."""
        import builder.agents.build as build_mod

        captured: dict[str, Any] = {}

        def _fake_build(engine: Any, **kw: Any) -> dict[str, Any]:
            captured.update(kw)
            return {}

        monkeypatch.setattr(build_mod, "run_interactive_build", _fake_build)
        run_build(BuildMode.PIPELINE, _stub_engine(), initial_prompt="ignored here")

        assert "initial_prompt" not in captured

    def test_resume_provenance_reaches_the_pipeline_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same fact must reach the other arm, or the banner lies there instead."""
        import builder.agents.build as build_mod

        captured: dict[str, Any] = {}

        def _fake_build(engine: Any, **kw: Any) -> dict[str, Any]:
            captured.update(kw)
            return {}

        monkeypatch.setattr(build_mod, "run_interactive_build", _fake_build)
        run_build(BuildMode.PIPELINE, _stub_engine(), resumed=True)

        assert captured["resumed"] is True


class TestPipelineModelOverrides:
    """`--model` / `--provider` / `--api-base` must reach BOTH arms (#399).

    They were threaded into the ReAct branch and dropped on the pipeline branch,
    while the pipeline arm still calls a model — resolving it from the ENVIRONMENT
    instead. So a "same-model" A/B ran the two arms on two different models and
    part of any measured token delta was a model delta.
    """

    def _capture_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import builder.agents.build as build_mod

        captured: dict[str, Any] = {}

        def _fake_build(engine: Any, **kw: Any) -> dict[str, Any]:
            captured["kw"] = kw
            return {}

        monkeypatch.setattr(build_mod, "run_interactive_build", _fake_build)
        return captured

    def test_pipeline_forwards_the_model_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from builder.agents.llm import ModelOverrides

        captured = self._capture_pipeline(monkeypatch)

        run_build(
            BuildMode.PIPELINE,
            _stub_engine(),
            provider="openai",
            model="gpt-5.6-luna",
            base_url="https://example.invalid/v1",
            output=print,
        )

        assert captured["kw"]["overrides"] == ModelOverrides(
            provider="openai", model="gpt-5.6-luna", base_url="https://example.invalid/v1"
        )

    def test_pipeline_with_no_overrides_forwards_an_empty_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HONESTY CONTROL — the default path must stay environment-resolved.

        An empty override set must not pin the model to anything; it has to leave
        every field None so `_build_chat_model` resolves from the environment
        exactly as before.
        """
        from builder.agents.llm import ModelOverrides

        captured = self._capture_pipeline(monkeypatch)

        run_build(BuildMode.PIPELINE, _stub_engine(), output=print)

        assert captured["kw"]["overrides"] == ModelOverrides()
        assert captured["kw"]["overrides"].is_empty()


class TestGeneratorRecordsArchitecture:
    """The exported crate says which arm built it (#772).

    ``GeneratorInfo.architecture`` existed but nothing ever set it, and the crate
    mapping never emitted it, so a pipeline crate and a ReAct crate were
    indistinguishable from their own metadata.
    """

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

    def test_pipeline_build_records_pipeline(self, tmp_path: Path) -> None:
        from builder.agents.build import run_interactive_build

        out = tmp_path / "crate"
        engine = self._engine(out)

        run_interactive_build(engine, pipeline_runner=lambda *a, **k: {"ok": True, "issues": []})

        assert self._run_properties(out)["Build architecture"] == "pipeline"

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

        run_build(BuildMode.REACT, engine)
        # The real ReAct loop exports from inside itself; the stub above cannot,
        # so drive the same export it would have run.
        export_crate(engine.state, str(out))

        assert self._run_properties(out)["Build architecture"] == "react"
