"""The ``BuildMode`` switch — the single A/B mode selector (Issue #309, Step 3).

Both the CLI (``main.py``) and the eval harness flip between the deterministic
pipeline and the legacy ReAct loop through *one* enum + dispatch in
``builder.agents.build``, instead of each hard-wiring its own boolean/string. The
two modes drive the same engine + toolbox; only orchestration differs (AGENTS.md
§1, D15 — both stay first-class).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from builder.agents.build import BuildMode, run_build

if TYPE_CHECKING:
    from builder.engine import AgentEngine


class TestBuildModeFromCli:
    def test_legacy_react_maps_to_react(self) -> None:
        assert BuildMode.from_cli(legacy_react=True) is BuildMode.REACT

    def test_default_maps_to_pipeline(self) -> None:
        assert BuildMode.from_cli(legacy_react=False) is BuildMode.PIPELINE

    def test_values_match_eval_arch_strings(self) -> None:
        """The eval's ``--arch`` choices and the BuildMode enum values MUST stay in
        lockstep so ``BuildMode(args.arch)`` always resolves. Assert against the REAL
        parser choices — not inline literals — so a change to either side that breaks
        the mapping (a renamed/added arch, a dropped enum value) fails here.
        """
        from eval.__main__ import build_arg_parser

        parser = build_arg_parser()
        arch = next(a for a in parser._actions if "--arch" in a.option_strings)
        assert set(arch.choices) == {m.value for m in BuildMode}
        # Every CLI choice round-trips through the shared enum.
        for choice in arch.choices:
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

        result = run_build(BuildMode.PIPELINE, object(), output=print)

        assert captured["kw"].get("output") is print
        assert result == {"pipeline": {}, "guidance": None}

    def test_react_dispatches_to_agent_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.react.agent_loop as agent_loop

        captured: dict[str, Any] = {}

        def _fake_agent(engine: Any, **kw: Any) -> str:
            captured.update(kw)
            return "return-value-is-ignored"

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _fake_agent)

        result = run_build(BuildMode.REACT, object(), provider="openai", model="m", base_url="u")

        # ReAct-only kwargs are forwarded; the loop mutates state in place, so
        # run_build returns None rather than a structured pipeline result.
        assert captured == {"provider": "openai", "model": "m", "base_url": "u"}
        assert result is None


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
            cast("AgentEngine", object()),
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

        run_build(BuildMode.PIPELINE, cast("AgentEngine", object()), output=print)

        assert captured["kw"]["overrides"] == ModelOverrides()
        assert captured["kw"]["overrides"].is_empty()
