"""Runner tests — fully offline via a mock agent factory.

The mock returns canned, REQUIRED-clean ``CrateState`` objects, so these exercise
the runner / metrics / determinism / report-shaping logic end to end **without a
live model or the network**. ``build_and_validate`` is heavy, so the module is
under a 120s timeout per the harness conventions.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from eval.agent_api import BuildOutcome
from eval.corpus import DEFAULT_CORPUS, EvalCase
from eval.runner import CaseResult, EvalReport, run_eval

pytestmark = pytest.mark.timeout(120)


# --------------------------------------------------------------------------- #
# Mock agents
# --------------------------------------------------------------------------- #


class _CleanMockAgent:
    """Returns a REQUIRED-clean state per case (deterministic)."""

    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id=None)


def _clean_factory() -> _CleanMockAgent:
    return _CleanMockAgent()


class _FailingMockAgent:
    """Returns an empty state that cannot reach conformance."""

    def build(self, case: EvalCase) -> BuildOutcome:
        return BuildOutcome(state=CrateState(), session_id=None)


def _failing_factory() -> _FailingMockAgent:
    return _FailingMockAgent()


class _NonDeterministicMockAgent:
    """Returns a *different* crate on each instance — a non-reproducible agent.

    The runner builds a *fresh* agent per repeat (so per-instance state can't leak
    into the determinism signal). To model genuine non-determinism the divergence
    must therefore be per-instance, not per-call — here a unique title per build.
    """

    def build(self, case: EvalCase) -> BuildOutcome:
        import uuid

        tag = uuid.uuid4().hex
        state = CrateState()
        state.metadata.title = f"run-{tag}"
        state.add_entity(
            Entity(
                entity_id="inv",
                type="Investigation",
                fields={"name": f"run-{tag}"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        return BuildOutcome(state=state, session_id=None)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestRunEvalShape:
    def test_returns_report_with_one_result_per_case(self) -> None:
        corpus = DEFAULT_CORPUS[:1]
        report = run_eval(_clean_factory, corpus, repeats=2)
        assert isinstance(report, EvalReport)
        assert len(report.results) == 1
        assert isinstance(report.results[0], CaseResult)

    def test_records_label_and_repeats(self) -> None:
        report = run_eval(_clean_factory, DEFAULT_CORPUS[:1], repeats=3, label="mock-x")
        assert report.label == "mock-x"
        assert report.repeats == 3
        assert report.results[0].repeats == 3


class TestSuccessAndConformance:
    def test_clean_agent_succeeds_with_full_conformance(self) -> None:
        report = run_eval(_clean_factory, DEFAULT_CORPUS[:1], repeats=1)
        res = report.results[0]
        assert res.success is True
        assert res.conformance == {"base": True, "isa": True, "tox": True}

    def test_failing_agent_does_not_succeed(self) -> None:
        report = run_eval(_failing_factory, DEFAULT_CORPUS[:1], repeats=1)
        res = report.results[0]
        assert res.success is False
        # An empty crate has no ISA backbone, so the ISA layer never conforms.
        assert res.conformance.get("isa") is not True


class TestDeterminism:
    def test_identical_repeats_are_deterministic(self) -> None:
        report = run_eval(_clean_factory, DEFAULT_CORPUS[:1], repeats=2)
        res = report.results[0]
        assert res.deterministic is True
        assert len(set(res.crate_hashes)) == 1
        assert len(res.crate_hashes) == 2

    def test_single_repeat_reports_determinism_as_none(self) -> None:
        # With one repeat there is nothing to compare; determinism is undefined.
        report = run_eval(_clean_factory, DEFAULT_CORPUS[:1], repeats=1)
        assert report.results[0].deterministic is None

    def test_diverging_repeats_are_not_deterministic(self) -> None:
        report = run_eval(_NonDeterministicMockAgent, DEFAULT_CORPUS[:1], repeats=2)
        res = report.results[0]
        assert res.deterministic is False
        assert len(set(res.crate_hashes)) == 2


class TestProfileBackedMetrics:
    def test_metrics_are_mined_from_an_injected_profile_reader(self) -> None:
        canned = {
            "ignored": [
                {"event": "node_end", "node": "model", "iteration": 4,
                 "input_tokens": 1000, "output_tokens": 200},
                {"event": "tool_call", "tool": "scan_files", "iteration": 1},
                {"event": "tool_call", "tool": "draft_investigation", "iteration": 2},
                {"event": "tool_call", "tool": "build_and_validate", "iteration": 4},
            ]
        }

        class _ProfiledAgent:
            def build(self, case: EvalCase) -> BuildOutcome:
                factory = case.build_state or CrateState
                return BuildOutcome(state=factory(), session_id="ignored")

        report = run_eval(
            lambda: _ProfiledAgent(),
            DEFAULT_CORPUS[:1],
            repeats=1,
            profile_reader=lambda sid: canned.get(sid, []),
        )
        res = report.results[0]
        assert res.input_tokens == 1000
        assert res.output_tokens == 200
        assert res.total_tokens == 1200
        assert res.tool_calls == 3
        assert res.iterations == 4

    def test_no_session_id_yields_zero_token_metrics(self) -> None:
        report = run_eval(_clean_factory, DEFAULT_CORPUS[:1], repeats=1)
        res = report.results[0]
        assert res.input_tokens == 0
        assert res.tool_calls == 0


class TestLatency:
    def test_latency_is_recorded_and_non_negative(self) -> None:
        report = run_eval(_clean_factory, DEFAULT_CORPUS[:1], repeats=1)
        assert report.results[0].latency_seconds >= 0.0


class TestErrorHandling:
    def test_a_raising_build_is_captured_as_a_failure_not_a_crash(self) -> None:
        class _RaisingAgent:
            def build(self, case: EvalCase) -> BuildOutcome:
                raise RuntimeError("boom")

        report = run_eval(lambda: _RaisingAgent(), DEFAULT_CORPUS[:1], repeats=1)
        res = report.results[0]
        assert res.success is False
        assert res.error is not None
        assert "boom" in res.error


class TestAggregateSummary:
    def test_summary_reports_success_rate_and_token_means(self) -> None:
        report = run_eval(_clean_factory, DEFAULT_CORPUS, repeats=2)
        summary = report.summary()
        assert summary["label"] == report.label
        assert summary["num_cases"] == len(DEFAULT_CORPUS)
        assert summary["success_rate"] == 1.0
        assert "mean_total_tokens" in summary
        assert "median_latency_seconds" in summary
        assert 0.0 <= summary["determinism_rate"] <= 1.0
