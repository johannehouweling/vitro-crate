"""Tests for the ``python -m eval`` entry point — offline (mock factory).

The CLI's default factory is the LIVE ReAct agent, so these inject a mock factory
and a canned profile reader to drive the whole pipeline (parse args -> run -> write
report) without a model. Only the wiring and the on-disk artifact are asserted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.state import CrateState
from eval.__main__ import build_arg_parser, run_main
from eval.agent_api import BuildOutcome
from eval.corpus import EvalCase

pytestmark = pytest.mark.timeout(120)


class _MockAgent:
    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id=None)


def _mock_factory() -> _MockAgent:
    return _MockAgent()


class TestArgParser:
    def test_defaults(self) -> None:
        args = build_arg_parser().parse_args([])
        assert args.label == "react-baseline"
        assert args.repeats == 2

    def test_overrides(self) -> None:
        args = build_arg_parser().parse_args(
            ["--label", "pipeline", "--repeats", "3", "--out", "x.ndjson"]
        )
        assert args.label == "pipeline"
        assert args.repeats == 3
        assert args.out == "x.ndjson"


class TestRunMain:
    def test_writes_a_labeled_report_and_returns_zero(self, tmp_path: Path) -> None:
        out = tmp_path / "report.ndjson"
        rc = run_main(
            ["--label", "mock-run", "--repeats", "2", "--out", str(out)],
            agent_factory=_mock_factory,
            profile_reader=lambda sid: [],
        )
        assert rc == 0
        assert out.exists()
        lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        summary = next(line for line in lines if line.get("record") == "summary")
        assert summary["label"] == "mock-run"
        assert summary["success_rate"] == 1.0

    def test_default_out_path_is_label_derived(self, tmp_path: Path) -> None:
        rc = run_main(
            ["--label", "mock-run"],
            agent_factory=_mock_factory,
            profile_reader=lambda sid: [],
            out_dir=str(tmp_path),
        )
        assert rc == 0
        produced = list(tmp_path.glob("*.ndjson"))
        assert len(produced) == 1
        assert "mock-run" in produced[0].name
