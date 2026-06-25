"""Config -> env hydration for the LIVE ``python -m eval`` path (#179).

The live ReAct factory reads provider/api_key/base_url/model from the
environment only (``builder.agents.agent_loop``). Credentials kept solely in
``~/.config/vitro-crate/config.toml`` must therefore be bridged into ``os.environ``
before the live run — exactly as the normal CLI does via
``merge_with_env(load_config())`` — otherwise a live run raises
"No LLM provider configured" despite valid creds on disk.

These tests assert the harness performs that hydration on the live path and that
the offline (injected ``agent_factory``) path stays config-free.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

import eval.__main__ as eval_main
from builder.state import CrateState
from eval.agent_api import BuildOutcome
from eval.corpus import EvalCase

pytestmark = pytest.mark.timeout(120)


class _MockAgent:
    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id=None)


def _mock_factory() -> _MockAgent:
    return _MockAgent()


class TestLiveConfigHydration:
    @pytest.mark.parametrize("arch", ["react", "pipeline"])
    def test_live_path_hydrates_config_into_env(
        self, arch: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """On the live path (no injected factory) the harness must bridge
        config.toml -> env via ``merge_with_env(load_config())`` before running,
        so a live run finds creds that live only in the config file.

        This must hold for **both** architectures: the ReAct engine and the
        deterministic pipeline both call a model now — the pipeline's drafter-leaf
        (``builder/agents/leaves.py``) builds a chat model from env vars
        (post-#211). Gating hydration on ``--arch react`` would leave a live
        ``--arch pipeline`` run with no creds, silently no-op the leaf, and yield
        a false-negative A/B comparison (#179).
        """
        calls: dict[str, int] = {"load": 0, "merge": 0}

        def fake_load_config() -> dict[str, Any]:
            calls["load"] += 1
            return {"openai": {"api_key": "sk-from-config"}}

        def fake_merge_with_env(cfg: dict[str, Any]) -> dict[str, Any]:
            calls["merge"] += 1
            return cfg

        monkeypatch.setattr(eval_main, "load_config", fake_load_config, raising=False)
        monkeypatch.setattr(eval_main, "merge_with_env", fake_merge_with_env, raising=False)

        # Stub BOTH live factories so no real LLM/network is contacted: each
        # returns a mock agent that builds an empty state. We are only asserting
        # hydration, which happens before factory selection.
        def fake_make_react_agent_factory(**_: Any) -> Callable[[], _MockAgent]:
            return _mock_factory

        def fake_make_pipeline_agent_factory() -> Callable[[], _MockAgent]:
            return _mock_factory

        import eval.pipeline_factory as pipeline_factory
        import eval.react_factory as react_factory

        monkeypatch.setattr(
            react_factory, "make_react_agent_factory", fake_make_react_agent_factory
        )
        monkeypatch.setattr(
            pipeline_factory,
            "make_pipeline_agent_factory",
            fake_make_pipeline_agent_factory,
        )

        rc = eval_main.run_main(
            ["--arch", arch, "--label", "live-hydration", "--repeats", "1"],
            profile_reader=lambda sid: [],
            out_dir=str(tmp_path),
        )

        assert rc == 0
        assert calls["load"] == 1, f"live --arch {arch} must load config.toml exactly once"
        assert calls["merge"] == 1, (
            f"live --arch {arch} must merge config into env exactly once"
        )

    def test_offline_path_does_not_require_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """When a caller injects an ``agent_factory`` (the offline/mock path), the
        harness must NOT load or merge config — offline tests stay config-free."""

        def boom_load_config() -> dict[str, Any]:
            raise AssertionError("offline path must not load config.toml")

        def boom_merge_with_env(cfg: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("offline path must not merge config into env")

        monkeypatch.setattr(eval_main, "load_config", boom_load_config, raising=False)
        monkeypatch.setattr(eval_main, "merge_with_env", boom_merge_with_env, raising=False)

        rc = eval_main.run_main(
            ["--label", "offline-run", "--repeats", "1"],
            agent_factory=_mock_factory,
            profile_reader=lambda sid: [],
            out_dir=str(tmp_path),
        )

        assert rc == 0
