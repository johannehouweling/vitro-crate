"""Cost + model-name capture (trap 5, #331).

The A/B differentiator is efficiency (tokens, wall-clock, $) and clean
termination, not raw capability. Tokens / wall-clock / step-count are already
recorded; this adds the model name (mined from ``profile.ndjson``) and the USD
cost. An unpriced model records ``cost_usd = None`` (never a guessed price); a
``price_override`` prices any model — the path the live gpt-5.6-luna re-run uses,
since that model is deliberately not committed to the public price table.

Offline: canned profile records + a mock agent, no LLM.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState
from eval.agent_api import BuildOutcome
from eval.corpus import DEFAULT_CORPUS, EvalCase
from eval.metrics import compute_cost, mine_profile_metrics
from eval.runner import run_eval

pytestmark = pytest.mark.timeout(120)


class TestMineModelName:
    def test_model_name_is_mined_from_model_node_ends(self) -> None:
        records = [
            {
                "event": "node_end",
                "node": "model",
                "input_tokens": 10,
                "output_tokens": 5,
                "model_name": "gpt-4o",
            }
        ]
        assert mine_profile_metrics(records).model_name == "gpt-4o"

    def test_model_name_none_when_absent(self) -> None:
        assert mine_profile_metrics([{"event": "tool_call", "tool": "x"}]).model_name is None


class TestComputeCost:
    def test_priced_model_uses_the_table(self) -> None:
        # gpt-4o = (2.50, 10.00) USD per 1M tokens.
        assert compute_cost(1_000_000, 1_000_000, "gpt-4o") == pytest.approx(12.50)

    def test_unknown_model_returns_none(self) -> None:
        assert compute_cost(1000, 1000, "totally-unknown-model") is None

    def test_none_model_returns_none(self) -> None:
        assert compute_cost(1000, 1000, None) is None

    def test_price_override_prices_any_model(self) -> None:
        cost = compute_cost(1_000_000, 0, "gpt-5.6-luna", price_override=(3.0, 15.0))
        assert cost == pytest.approx(3.0)

    def test_dated_suffix_matches_longest_prefix(self) -> None:
        # A dated variant matches its base model; the most specific name wins so
        # "gpt-4o-mini-*" is never mispriced as "gpt-4o".
        assert compute_cost(1_000_000, 0, "gpt-4o-2024-08-06") == compute_cost(
            1_000_000, 0, "gpt-4o"
        )
        assert compute_cost(1_000_000, 0, "gpt-4o-mini-2024-07-18") == compute_cost(
            1_000_000, 0, "gpt-4o-mini"
        )


class _ProfiledAgent:
    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id="sid")


def _gpt4o_reader(_sid: str) -> list[dict]:
    return [
        {
            "event": "node_end",
            "node": "model",
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "model_name": "gpt-4o",
        }
    ]


class TestRunnerCapturesCost:
    def test_case_result_carries_model_and_cost(self) -> None:
        report = run_eval(
            lambda: _ProfiledAgent(), DEFAULT_CORPUS[:1], repeats=1, profile_reader=_gpt4o_reader
        )
        res = report.results[0]
        assert res.model_name == "gpt-4o"
        assert res.cost_usd == pytest.approx(2.50)
        assert res.to_dict()["cost_usd"] == pytest.approx(2.50)

    def test_summary_totals_cost(self) -> None:
        report = run_eval(
            lambda: _ProfiledAgent(), DEFAULT_CORPUS[:2], repeats=1, profile_reader=_gpt4o_reader
        )
        assert report.summary()["total_cost_usd"] == pytest.approx(5.00)

    def test_unpriced_run_totals_cost_as_none(self) -> None:
        def reader(_sid: str) -> list[dict]:
            return [
                {
                    "event": "node_end",
                    "node": "model",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "model_name": "mystery-model",
                }
            ]

        report = run_eval(
            lambda: _ProfiledAgent(), DEFAULT_CORPUS[:1], repeats=1, profile_reader=reader
        )
        assert report.results[0].cost_usd is None
        # No case had a known price → total is None, not a misleading $0.
        assert report.summary()["total_cost_usd"] is None

    def test_price_override_threads_through_run_eval(self) -> None:
        def reader(_sid: str) -> list[dict]:
            return [
                {
                    "event": "node_end",
                    "node": "model",
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "model_name": "gpt-5.6-luna",
                }
            ]

        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=1,
            profile_reader=reader,
            price_override=(4.0, 20.0),
        )
        assert report.results[0].cost_usd == pytest.approx(4.0)
