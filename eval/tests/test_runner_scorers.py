"""Runner wiring for the manuscript MIT axis (#474) and AI-readiness (#657).

The success predicate stays untouched; both are additive signals in the
``meets_quota`` mould, computed from the assembled graph: ``mit_propertyid``
and the ``air_*`` Bridge2AI coverage.

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


class _MockAgent:
    """Builds the case's canned state."""

    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id=None)


class TestScorerAxesRecorded:
    @pytest.fixture(scope="class")
    def react_result(self):
        report = run_eval(lambda: _MockAgent(), [_MINIMAL_CASE], repeats=2)
        return report.results[0]

    def test_mit_axis_is_always_scored(self, react_result) -> None:
        assert isinstance(react_result.mit_propertyid, float)
        assert 0.0 <= react_result.mit_propertyid <= 1.0
        assert len(react_result.mit_propertyid_per_repeat) == 2

    def test_ai_readiness_is_scored_from_the_graph(self, react_result) -> None:
        """AI-readiness reads the assembled graph, so every crate is assessable."""
        assert isinstance(react_result.air_met, int)
        assert isinstance(react_result.air_assessed, int)
        assert 0 <= react_result.air_met <= react_result.air_assessed <= 28
        assert len(react_result.air_met_per_repeat) == 2

    def test_the_ai_readiness_profile_has_seven_dimensions_and_no_total(
        self, react_result
    ) -> None:
        """The authors refuse a cross-dimension aggregate; a column would re-add one."""
        dimensions = react_result.air_detail["dimensions"]
        assert [d["dimension"] for d in dimensions] == [0, 1, 2, 3, 4, 5, 6]
        for dim in dimensions:
            assert dim["published_pct"] is not None, "theirs is always computable"
        line = react_result.to_dict()
        assert "air" not in line, "a single `air` number is the metric this axis replaced"

    def test_axes_serialize_into_the_case_line(self, react_result) -> None:
        line = react_result.to_dict()
        assert "mit_propertyid" in line
        assert "mit_propertyid_per_repeat" in line
        assert "mit_propertyid_detail" in line
        assert "air_met" in line
        assert "air_assessed" in line
        assert "air_met_per_repeat" in line
        assert "air_detail" in line


class TestAxesInComparison:
    def test_compare_reports_carries_the_mit_axis(self) -> None:
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
            mit_propertyid_per_repeat=[0.25],
        )
        report = EvalReport(label="a", repeats=1, results=[result])
        cases = compare_reports(report)["cases"]["c1"]["a"]
        assert cases["mit_propertyid"] == 0.25
