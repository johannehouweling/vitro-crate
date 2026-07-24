"""The ``BuildMode`` switch — the single A/B mode selector (Issue #309, Step 3).

Both the CLI (``main.py``) and the eval harness flip between the deterministic
pipeline and the legacy ReAct loop through *one* enum + dispatch in
``builder.agents.build``, instead of each hard-wiring its own boolean/string. The
two modes drive the same engine + toolbox; only orchestration differs (AGENTS.md
§1, D15 — both stay first-class).
"""

from __future__ import annotations

from typing import Any

import pytest

from builder.agents.build import BuildMode, run_build


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
