"""Per-repeat variance capture (the open half of trap 4, #331 / #335).

``--repeats`` defaults to 3, but the runner used to persist only repeat #1's
tokens / latency / stop-reason for every headline metric; the other repeats fed
only the determinism-hash boolean. For the stochastic ReAct arm one draw is
noisy (measured CV runs as high as ~0.6–0.7), so the report has to surface the
spread across *all* repeats, not a single sample.

These tests pin the additive contract: the representative (repeat #1) fields are
unchanged, and the runner now also records each repeat's tokens / latency /
stop-reason plus a mean ± spread block. Offline: a stateful profile reader models
genuine per-repeat divergence, no LLM.
"""

from __future__ import annotations

import statistics

import pytest

from builder.state import CrateState
from eval.agent_api import BuildOutcome
from eval.corpus import DEFAULT_CORPUS, EvalCase
from eval.runner import run_eval

pytestmark = pytest.mark.timeout(120)


class _ProfiledAgent:
    """A mock agent that profiles under a fixed session id.

    The runner reads the profile once per repeat, so a *stateful* reader keyed on
    call order (below) is what injects genuine per-repeat variance even though the
    session id is constant.
    """

    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id="sid")


def _varying_reader(token_counts: list[int]):
    """Return a profile reader that yields a different input-token count per call.

    Call ``k`` reports ``token_counts[k]`` input tokens (and 0 output), modelling a
    stochastic arm whose per-repeat token usage differs run to run.
    """
    calls = {"n": 0}

    def reader(_sid: str) -> list[dict]:
        i = calls["n"]
        calls["n"] += 1
        value = token_counts[i] if i < len(token_counts) else token_counts[-1]
        return [
            {
                "event": "node_end",
                "node": "model",
                "input_tokens": value,
                "output_tokens": 0,
                "model_name": "gpt-5.6-luna",
            }
        ]

    return reader


class TestPerRepeatTokenCapture:
    def test_records_total_tokens_for_every_repeat(self) -> None:
        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=3,
            profile_reader=_varying_reader([100, 300, 200]),
        )
        res = report.results[0]
        assert res.total_tokens_per_repeat == [100, 300, 200]

    def test_records_latency_for_every_repeat(self) -> None:
        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=3,
            profile_reader=_varying_reader([100, 300, 200]),
        )
        res = report.results[0]
        assert len(res.latency_per_repeat) == 3
        assert all(x >= 0.0 for x in res.latency_per_repeat)

    def test_records_stop_reason_for_every_repeat(self) -> None:
        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=3,
            profile_reader=_varying_reader([100, 300, 200]),
        )
        res = report.results[0]
        assert len(res.stop_reasons) == 3


class TestRepresentativeFieldsUnchanged:
    def test_headline_metrics_still_come_from_repeat_one(self) -> None:
        # Backwards compatible: the representative token count is repeat #1's, not
        # the mean, so existing consumers (cost, dashboards) read the same field.
        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=3,
            profile_reader=_varying_reader([100, 300, 200]),
        )
        res = report.results[0]
        assert res.total_tokens == 100
        assert res.total_tokens == res.total_tokens_per_repeat[0]


class TestVarianceBlock:
    def test_to_dict_exposes_a_token_variance_spread(self) -> None:
        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=3,
            profile_reader=_varying_reader([100, 300, 200]),
        )
        variance = report.results[0].to_dict()["variance"]
        tok = variance["total_tokens"]
        assert tok["mean"] == pytest.approx(200.0)
        assert tok["min"] == 100
        assert tok["max"] == 300
        assert tok["stdev"] == pytest.approx(statistics.stdev([100, 300, 200]))

    def test_to_dict_exposes_a_latency_variance_spread(self) -> None:
        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=2,
            profile_reader=_varying_reader([100, 200]),
        )
        variance = report.results[0].to_dict()["variance"]
        assert set(variance["latency_seconds"]) == {"mean", "min", "max", "stdev"}

    def test_single_repeat_has_zero_spread(self) -> None:
        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=1,
            profile_reader=_varying_reader([123]),
        )
        tok = report.results[0].to_dict()["variance"]["total_tokens"]
        assert tok["mean"] == pytest.approx(123.0)
        assert tok["stdev"] == 0.0


class TestSummaryVariance:
    def test_summary_reports_mean_token_cv(self) -> None:
        # A stochastic arm has a non-zero coefficient of variation; the summary
        # surfaces it as a single arm-level "how noisy" number.
        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=3,
            profile_reader=_varying_reader([100, 300, 200]),
        )
        summary = report.summary()
        expected_cv = statistics.stdev([100, 300, 200]) / 200.0
        assert summary["mean_total_tokens_cv"] == pytest.approx(expected_cv)

    def test_constant_arm_has_zero_cv(self) -> None:
        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=3,
            profile_reader=_varying_reader([500, 500, 500]),
        )
        assert report.summary()["mean_total_tokens_cv"] == pytest.approx(0.0)
