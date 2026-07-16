"""Tests for the ``--arch react|pipeline`` selection in ``python -m eval``.

ReAct stays the DEFAULT; ``--arch pipeline`` opts into the deterministic spine.
These assert the arg parsing and that the chosen architecture selects the right
factory, without contacting a model (the factory-selection helper is exercised
directly so no live build runs).
"""

from __future__ import annotations

import pytest

from builder.agents.build import BuildMode
from eval.__main__ import build_arg_parser, select_agent_factory


class TestArchArg:
    def test_default_arch_is_react(self) -> None:
        args = build_arg_parser().parse_args([])
        assert args.arch == "react"

    def test_arch_pipeline_is_selectable(self) -> None:
        args = build_arg_parser().parse_args(["--arch", "pipeline"])
        assert args.arch == "pipeline"

    def test_arch_rejects_unknown(self) -> None:
        with pytest.raises(SystemExit):
            build_arg_parser().parse_args(["--arch", "bogus"])


class TestSelectAgentFactory:
    def test_react_selects_react_factory(self) -> None:
        from eval.react_factory import ReActBuildAgent

        factory = select_agent_factory(BuildMode.REACT, provider=None, model=None, base_url=None)
        assert isinstance(factory(), ReActBuildAgent)

    def test_pipeline_selects_pipeline_factory(self) -> None:
        from eval.pipeline_factory import PipelineBuildAgent

        factory = select_agent_factory(BuildMode.PIPELINE, provider=None, model=None, base_url=None)
        assert isinstance(factory(), PipelineBuildAgent)
