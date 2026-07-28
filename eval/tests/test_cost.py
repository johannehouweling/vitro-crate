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


def _varying_gpt4o_reader(input_tokens_per_call: list[int]):
    """Return a priced profile reader yielding a different token count per call.

    The runner reads the profile once per repeat, so call ``k`` reports
    ``input_tokens_per_call[k]`` — modelling a stochastic arm whose repeats cost
    genuinely *different* amounts. Deliberately unequal so a total can distinguish
    "sum of every repeat" from "repeat #1 multiplied by the repeat count".
    """
    calls = {"n": 0}

    def reader(_sid: str) -> list[dict]:
        i = calls["n"]
        calls["n"] += 1
        value = (
            input_tokens_per_call[i]
            if i < len(input_tokens_per_call)
            else input_tokens_per_call[-1]
        )
        return [
            {
                "event": "node_end",
                "node": "model",
                "input_tokens": value,
                "output_tokens": 0,
                "model_name": "gpt-4o",
            }
        ]

    return reader


# gpt-4o input is $2.50/Mtok, so 1M/2M/3M input tokens cost $2.50/$5.00/$7.50.
_UNEQUAL_REPEATS = [1_000_000, 2_000_000, 3_000_000]
_PER_REPEAT_COSTS = [2.50, 5.00, 7.50]


class TestCostAcrossRepeats:
    """``total_cost_usd`` must bill every repeat, not repeat #1 alone (#401).

    A 3-repeat run previously reported repeat #1's cost as the run total — a ~3x
    understatement of the headline efficiency number the whole A/B rests on.
    """

    def _report(self, repeats: int = 3):
        return run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=repeats,
            profile_reader=_varying_gpt4o_reader(_UNEQUAL_REPEATS),
        )

    def test_case_records_the_cost_of_every_repeat(self) -> None:
        res = self._report().results[0]
        assert res.cost_usd_per_repeat == pytest.approx(_PER_REPEAT_COSTS)

    def test_case_total_cost_sums_all_repeats(self) -> None:
        res = self._report().results[0]
        assert res.total_cost_usd == pytest.approx(15.00)

    def test_summary_total_is_actual_spend_not_repeat_one(self) -> None:
        # The bug: this reported 2.50 (repeat #1) for a run that spent 15.00.
        summary = self._report().summary()
        assert summary["total_cost_usd"] == pytest.approx(15.00)

    def test_summary_total_is_not_repeat_one_scaled_by_repeat_count(self) -> None:
        # Guards against the tempting wrong fix: repeats * cost_usd == 7.50, which
        # is right only when every repeat costs the same. Real repeats do not.
        summary = self._report().summary()
        assert summary["total_cost_usd"] != pytest.approx(2.50 * 3)

    def test_summary_reports_mean_cost_per_repeat_for_comparability(self) -> None:
        # Comparable across runs that used different --repeats values.
        summary = self._report().summary()
        assert summary["mean_cost_usd_per_repeat"] == pytest.approx(5.00)

    def test_representative_cost_usd_stays_repeat_one(self) -> None:
        # Additive contract, matching total_tokens (#335): the headline per-case
        # field remains one representative build so reprice/dashboards are unmoved.
        res = self._report().results[0]
        assert res.cost_usd == pytest.approx(2.50)
        assert res.cost_usd == pytest.approx(res.cost_usd_per_repeat[0])

    def test_to_dict_carries_the_per_repeat_costs(self) -> None:
        as_dict = self._report().results[0].to_dict()
        assert as_dict["cost_usd_per_repeat"] == pytest.approx(_PER_REPEAT_COSTS)
        assert as_dict["total_cost_usd"] == pytest.approx(15.00)

    def test_single_repeat_total_equals_the_one_cost(self) -> None:
        # Honesty control: with one repeat the fix must change nothing at all.
        summary = self._report(repeats=1).summary()
        assert summary["total_cost_usd"] == pytest.approx(2.50)
        assert summary["mean_cost_usd_per_repeat"] == pytest.approx(2.50)

    def test_unpriced_model_totals_none_across_repeats(self) -> None:
        # Honesty control: an unpriced run still reads "cost unknown", never $0.
        def reader(_sid: str) -> list[dict]:
            return [
                {
                    "event": "node_end",
                    "node": "model",
                    "input_tokens": 1_000,
                    "output_tokens": 0,
                    "model_name": "mystery-model",
                }
            ]

        report = run_eval(
            lambda: _ProfiledAgent(), DEFAULT_CORPUS[:1], repeats=3, profile_reader=reader
        )
        assert report.results[0].cost_usd_per_repeat == [None, None, None]
        assert report.results[0].total_cost_usd is None
        assert report.summary()["total_cost_usd"] is None
        assert report.summary()["mean_cost_usd_per_repeat"] is None
