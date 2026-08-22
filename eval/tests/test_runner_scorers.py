"""Runner wiring for the manuscript scorer axes (#474).

The success predicate stays untouched; the two axes are additive signals in
the ``meets_quota`` mould: ``mit_propertyid`` (always computable from the
assembled graph) and ``csvw_typing`` (``None`` — "not assessed" — for an arm
that produced no pipeline condition-table report, e.g. ReAct).

Fully offline via mock agents; ``build_and_validate`` runs, so the module
carries the harness 120s timeout.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState
from eval.agent_api import BuildOutcome
from eval.corpus import DEFAULT_CORPUS, EvalCase
from eval.runner import run_eval

pytestmark = pytest.mark.timeout(120)

_MINIMAL_CASE = next(c for c in DEFAULT_CORPUS if c.case_id == "minimal-backbone")


class _ReActLikeAgent:
    """No ``pipeline_result`` — the axis must read "not assessed", never 0."""

    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id=None)


class _PipelineLikeAgent:
    """Carries a pipeline report whose condition table refused to populate."""

    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(
            state=factory(),
            session_id=None,
            pipeline_result={
                "materialized": {
                    "condition_table": {
                        "populated": False,
                        "rows": 0,
                        "path": "",
                        "reason": "no candidate",
                    }
                }
            },
        )


class TestScorerAxesRecorded:
    @pytest.fixture(scope="class")
    def react_result(self):
        report = run_eval(lambda: _ReActLikeAgent(), [_MINIMAL_CASE], repeats=2)
        return report.results[0]

    @pytest.fixture(scope="class")
    def pipeline_result(self):
        report = run_eval(lambda: _PipelineLikeAgent(), [_MINIMAL_CASE], repeats=1)
        return report.results[0]

    def test_mit_axis_is_always_scored(self, react_result) -> None:
        assert isinstance(react_result.mit_propertyid, float)
        assert 0.0 <= react_result.mit_propertyid <= 1.0
        assert len(react_result.mit_propertyid_per_repeat) == 2

    def test_csvw_axis_not_assessed_without_pipeline_report(self, react_result) -> None:
        assert react_result.csvw_typing is None
        assert react_result.csvw_typing_per_repeat == [None, None]

    def test_csvw_axis_assessed_from_pipeline_report(self, pipeline_result) -> None:
        # An unpopulated table is a real (deflating) verdict, not "unknown".
        assert pipeline_result.csvw_typing == 0.0
        assert pipeline_result.csvw_typing_per_repeat == [0.0]

    def test_ai_readiness_is_scored_on_both_arms(self, react_result, pipeline_result) -> None:
        """AI-readiness reads the assembled graph, so no arm is exempt from it.

        Unlike `csvw_typing`, it does not depend on a pipeline condition-table
        report — a ReAct crate is as assessable as a pipeline one.
        """
        for result in (react_result, pipeline_result):
            assert isinstance(result.air_met, int)
            assert isinstance(result.air_assessed, int)
            assert 0 <= result.air_met <= result.air_assessed <= 28
        assert len(react_result.air_met_per_repeat) == 2

    def test_the_ai_readiness_profile_has_seven_dimensions_and_no_total(
        self, pipeline_result
    ) -> None:
        """The authors refuse a cross-dimension aggregate; a column would re-add one."""
        dimensions = pipeline_result.air_detail["dimensions"]
        assert [d["dimension"] for d in dimensions] == [0, 1, 2, 3, 4, 5, 6]
        for dim in dimensions:
            assert dim["published_pct"] is not None, "theirs is always computable"
        line = pipeline_result.to_dict()
        assert "air" not in line, "a single `air` number is the metric this axis replaced"

    def test_axes_serialize_into_the_case_line(self, react_result) -> None:
        line = react_result.to_dict()
        assert "mit_propertyid" in line
        assert "csvw_typing" in line
        assert "mit_propertyid_per_repeat" in line
        assert "csvw_typing_per_repeat" in line
        assert "mit_propertyid_detail" in line
        assert "air_met" in line
        assert "air_assessed" in line
        assert "air_met_per_repeat" in line
        assert "air_detail" in line


class TestAxesInComparison:
    def test_compare_reports_carries_both_axes(self) -> None:
        # compare_reports' per-case dict is a hand-maintained whitelist — a new
        # axis does not propagate automatically, so pin it (fully offline).
        from eval.report import compare_reports
        from eval.runner import CaseResult, EvalReport

        result = CaseResult(
            case_id="c1",
            kind="minimal",
            success=True,
            conformance={"base": True, "isa": True, "tox": True},
            issues=[],
            input_tokens=1,
            output_tokens=1,
            tool_calls=1,
            iterations=1,
            latency_seconds=0.1,
            crate_hashes=["abc"],
            deterministic=None,
            repeats=1,
            mit_propertyid=0.25,
            csvw_typing=None,
            mit_propertyid_per_repeat=[0.25],
            csvw_typing_per_repeat=[None],
        )
        report = EvalReport(label="a", repeats=1, results=[result])
        cases = compare_reports(report)["cases"]["c1"]["a"]
        assert cases["mit_propertyid"] == 0.25
        assert cases["csvw_typing"] is None
