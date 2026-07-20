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
        # repeats >= 3 so variance is reported over more than one or two samples
        # (trap 4, #331).
        assert args.repeats == 3
        assert args.price_input is None
        assert args.price_output is None
        assert args.max_transient_retries == 2

    def test_overrides(self) -> None:
        args = build_arg_parser().parse_args(
            ["--label", "pipeline", "--repeats", "5", "--out", "x.ndjson"]
        )
        assert args.label == "pipeline"
        assert args.repeats == 5
        assert args.out == "x.ndjson"

    def test_price_and_retry_flags(self) -> None:
        args = build_arg_parser().parse_args(
            ["--price-input", "3.0", "--price-output", "15.0", "--max-transient-retries", "4"]
        )
        assert args.price_input == 3.0
        assert args.price_output == 15.0
        assert args.max_transient_retries == 4


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

    def test_price_override_flows_into_the_report(self, tmp_path: Path) -> None:
        # The --price-* flags price an otherwise-unlisted model end to end (trap 5):
        # a profiled mock + canned reader report 1M input tokens on "mystery-model",
        # priced at $7/Mtok input via the override.
        out = tmp_path / "priced.ndjson"

        class _ProfiledMock:
            def build(self, case: EvalCase) -> BuildOutcome:
                factory = case.build_state or CrateState
                return BuildOutcome(state=factory(), session_id="sid")

        def reader(_sid: str) -> list[dict]:
            return [
                {
                    "event": "node_end",
                    "node": "model",
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "model_name": "mystery-model",
                }
            ]

        rc = run_main(
            [
                "--label",
                "priced",
                "--repeats",
                "1",
                "--price-input",
                "7.0",
                "--price-output",
                "21.0",
                "--out",
                str(out),
            ],
            agent_factory=lambda: _ProfiledMock(),
            profile_reader=reader,
        )
        assert rc == 0
        lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        case_line = next(line for line in lines if line.get("record") == "case")
        assert case_line["model_name"] == "mystery-model"
        assert case_line["cost_usd"] == pytest.approx(7.0)
