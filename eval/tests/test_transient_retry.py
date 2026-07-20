"""Transient-failure re-run (trap 1, #331).

A Connection / API / timeout error is a property of the network, not of the
architecture under test — counting it as a build failure produces a wrong verdict.
The runner re-runs a case a bounded number of times on a *transient* error before
it counts; a genuine (non-transient) failure is never retried.

Offline: mock agents whose successive instances behave as scripted, no LLM.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState
from eval.agent_api import BuildOutcome
from eval.corpus import DEFAULT_CORPUS, EvalCase
from eval.runner import _is_transient_error, run_eval

pytestmark = pytest.mark.timeout(120)


class TestTransientClassifier:
    @pytest.mark.parametrize(
        "message",
        [
            "Connection error.",
            "Request timed out.",
            "Read timed out.",
            "Rate limit reached for gpt-4o",
            "Too Many Requests",
            "overloaded_error: the model is overloaded",
            "503 Service Unavailable",
            "Bad gateway",
        ],
    )
    def test_transient_messages(self, message: str) -> None:
        assert _is_transient_error(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            None,
            "",
            "SHACL validation failed: missing required property",
            "KeyError: 'name'",
            "the crate is not conformant",
        ],
    )
    def test_non_transient_messages(self, message: str | None) -> None:
        assert _is_transient_error(message) is False


class TestTransientRetry:
    def test_transient_failure_is_retried_then_succeeds(self) -> None:
        attempts = {"n": 0}

        class _FlakyAgent:
            def build(self, case: EvalCase) -> BuildOutcome:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return BuildOutcome(
                        state=CrateState(),
                        session_id=None,
                        error="Connection error.",
                        stop_reason="error",
                    )
                factory = case.build_state or CrateState
                return BuildOutcome(state=factory(), session_id=None, stop_reason="completed")

        report = run_eval(lambda: _FlakyAgent(), DEFAULT_CORPUS[:1], repeats=1)
        res = report.results[0]
        assert attempts["n"] == 2  # one initial + one retry
        assert res.success is True
        assert res.transient_retries == 1
        assert res.stop_reason == "completed"
        assert res.error is None

    def test_non_transient_failure_is_not_retried(self) -> None:
        attempts = {"n": 0}

        class _HardFailAgent:
            def build(self, case: EvalCase) -> BuildOutcome:
                attempts["n"] += 1
                return BuildOutcome(
                    state=CrateState(),
                    session_id=None,
                    error="SHACL validation failed",
                    stop_reason="error",
                )

        report = run_eval(lambda: _HardFailAgent(), DEFAULT_CORPUS[:1], repeats=1)
        res = report.results[0]
        assert attempts["n"] == 1  # a genuine failure is measured, not retried
        assert res.transient_retries == 0
        assert res.error is not None

    def test_persistent_transient_failure_stops_after_max_retries(self) -> None:
        attempts = {"n": 0}

        class _AlwaysFlaky:
            def build(self, case: EvalCase) -> BuildOutcome:
                attempts["n"] += 1
                return BuildOutcome(
                    state=CrateState(),
                    session_id=None,
                    error="Read timed out.",
                    stop_reason="error",
                )

        report = run_eval(
            lambda: _AlwaysFlaky(), DEFAULT_CORPUS[:1], repeats=1, max_transient_retries=2
        )
        res = report.results[0]
        assert attempts["n"] == 3  # one initial + two retries, then it counts
        assert res.transient_retries == 2
        assert res.error is not None
        assert res.to_dict()["transient_retries"] == 2
