"""Stop-reason capture across the A/B (trap 2, #331).

A ReAct "win" at the recursion cap is not a clean win, so the harness records a
first-class ``stop_reason`` per run: ``"completed"`` (self-terminated), ``"cap_hit"``
(hit the LangGraph ``recursion_limit``), or ``"error"`` (the build raised). The
deterministic pipeline always self-terminates.

Offline: injected drivers/runners and a mock agent, so no LLM is contacted. A
``cap_hit`` deliberately preserves the partial crate — the conformance predicate
still measures what the run produced at the cutoff.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState
from eval.agent_api import BuildOutcome
from eval.corpus import DEFAULT_CORPUS, EvalCase, first_folder_case
from eval.pipeline_factory import PipelineBuildAgent
from eval.react_factory import ReActBuildAgent
from eval.runner import run_eval

pytestmark = pytest.mark.timeout(120)


class TestReActStopReason:
    def test_completed_when_the_loop_reports_a_clean_stop(self) -> None:
        agent = ReActBuildAgent(graph_driver=lambda e, p: "completed")
        outcome = agent.build(DEFAULT_CORPUS[0])
        assert outcome.stop_reason == "completed"
        assert outcome.error is None

    def test_cap_hit_is_reported_by_the_loop_not_raised(self) -> None:
        """The shipped loop catches a `GraphRecursionError` per turn and keeps
        the partial crate, so the cap can only reach the harness as a REPORTED
        stop reason — the eval used to catch an exception that (since the invoke
        moved behind the wall-clock guard) never arrives (#609)."""
        agent = ReActBuildAgent(graph_driver=lambda e, p: "cap_hit")
        outcome = agent.build(DEFAULT_CORPUS[0])
        assert outcome.stop_reason == "cap_hit"
        # cap_hit is not a hard build error: the partial crate is preserved so the
        # conformance predicate can still measure what the run produced at the cap.
        assert outcome.error is None
        assert isinstance(outcome.state, CrateState)

    def test_error_on_other_exception(self) -> None:
        def boom(engine, prompt):  # type: ignore[no-untyped-def]
            raise RuntimeError("model unreachable")

        agent = ReActBuildAgent(graph_driver=boom)
        outcome = agent.build(DEFAULT_CORPUS[0])
        assert outcome.stop_reason == "error"
        assert outcome.error is not None
        assert "model unreachable" in outcome.error


class TestPipelineStopReason:
    def test_completed_on_success(self) -> None:
        agent = PipelineBuildAgent(pipeline_runner=lambda e: {"ok": True})
        outcome = agent.build(first_folder_case())
        assert outcome.stop_reason == "completed"

    def test_error_on_exception(self) -> None:
        def boom(engine):  # type: ignore[no-untyped-def]
            raise RuntimeError("pipeline exploded")

        agent = PipelineBuildAgent(pipeline_runner=boom)
        outcome = agent.build(first_folder_case())
        assert outcome.stop_reason == "error"


class _StopReasonAgent:
    """A mock agent returning a fixed stop_reason (offline)."""

    def __init__(self, stop_reason: str) -> None:
        self._stop_reason = stop_reason

    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id=None, stop_reason=self._stop_reason)


class TestRunnerPropagatesStopReason:
    def test_case_result_carries_stop_reason(self) -> None:
        report = run_eval(lambda: _StopReasonAgent("cap_hit"), DEFAULT_CORPUS[:1], repeats=1)
        res = report.results[0]
        assert res.stop_reason == "cap_hit"
        assert res.to_dict()["stop_reason"] == "cap_hit"

    def test_summary_breaks_down_stop_reasons(self) -> None:
        report = run_eval(lambda: _StopReasonAgent("cap_hit"), DEFAULT_CORPUS, repeats=1)
        summary = report.summary()
        # Every case self-reported cap_hit here.
        assert summary["num_cap_hit"] == len(DEFAULT_CORPUS)
        assert summary["num_completed"] == 0
        assert summary["num_error"] == 0
