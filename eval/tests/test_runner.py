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


class _FlakyMockAgent:
    """Conforms on the FIRST build only; every later build fails (#405).

    The runner constructs a fresh agent per repeat, so the "which repeat am I"
    counter must be class-level. This is the exact defect shape: a 1-in-3 success
    that the repeat-#1-only predicate reported as a clean pass.
    """

    builds = 0

    def build(self, case: EvalCase) -> BuildOutcome:
        _FlakyMockAgent.builds += 1
        if _FlakyMockAgent.builds == 1:
            factory = case.build_state or CrateState
            return BuildOutcome(state=factory(), session_id=None)
        return BuildOutcome(state=CrateState(), session_id=None)


class TestSuccessAcrossRepeats:
    """#405 — validity must be measured on every repeat, not just the first.

    ``success_rate`` was the headline validity claim of the whole A/B and it was
    computed from repeat #1 alone, so a case that conformed once in three
    contributed a full 1.0 and its two failures left no trace. It skews
    asymmetrically: the deterministic pipeline genuinely is represented by one
    draw, the stochastic ReAct arm is not, and intermittent conformance is exactly
    ReAct's failure mode worth measuring.
    """

    # Built ONCE for the whole class. Scoring a repeat means a full 3-pass SHACL
    # `build_and_validate` (`reaches_isa_tox_conformance`), so a per-test rebuild
    # would run it 3x per test — the dominant cost here, since the mock build
    # itself is instant.
    @pytest.fixture(scope="class")
    def flaky(self) -> EvalReport:
        _FlakyMockAgent.builds = 0
        return run_eval(
            lambda: _FlakyMockAgent(),
            [DEFAULT_CORPUS[0]],
            label="flaky",
            repeats=3,
        )

    def test_every_repeat_is_evaluated_not_just_the_first(self, flaky) -> None:
        result = flaky.results[0]
        assert result.success_per_repeat == [True, False, False], (
            "the predicate must run on each repeat's state"
        )

    def test_case_success_rate_is_the_fraction_that_conformed(self, flaky) -> None:
        result = flaky.results[0]
        assert result.success_rate == pytest.approx(1 / 3)

    def test_a_one_in_three_case_does_not_report_a_perfect_arm_success_rate(self, flaky) -> None:
        """The bug, stated as a test: 1-of-3 must not read as 1.0."""
        summary = flaky.summary()
        assert summary["success_rate"] == pytest.approx(1 / 3), (
            "success_rate must span repeats, not report repeat #1's draw"
        )

    def test_the_strict_and_optimistic_counts_are_both_reported(self, flaky) -> None:
        """A flaky case counts as an any-repeat success but not an all-repeat one."""
        summary = flaky.summary()
        assert summary["num_success_all_repeats"] == 0
        assert summary["num_success_any_repeat"] == 1

    def test_always_succeeds_separates_flaky_from_solid(self, flaky) -> None:
        assert flaky.results[0].always_succeeds is False

        solid = run_eval(
            _clean_factory, [DEFAULT_CORPUS[0]], label="solid", repeats=3
        ).results[0]
        assert solid.always_succeeds is True
        assert solid.success_rate == pytest.approx(1.0)

    def test_conformance_and_quota_are_recorded_per_repeat(self, flaky) -> None:
        result = flaky.results[0]
        assert len(result.conformance_per_repeat) == 3
        assert len(result.meets_quota_per_repeat) == 3
        # Repeat #1 conformed on all three layers; the later ones did not.
        assert all(result.conformance_per_repeat[0].values())
        assert not all(result.conformance_per_repeat[1].values())

    def test_success_stays_repeat_ones_representative_value(self, flaky) -> None:
        """Consistent with cost_usd / total_tokens — the headline stays one draw."""
        result = flaky.results[0]
        assert result.success is True
        assert result.success_per_repeat[0] is True

    def test_a_non_conforming_arm_reports_zero_not_a_false_pass(self) -> None:
        summary = run_eval(
            _failing_factory, [DEFAULT_CORPUS[0]], label="bad", repeats=2
        ).summary()
        assert summary["success_rate"] == 0.0
        assert summary["num_success_any_repeat"] == 0

    def test_the_case_line_carries_the_spread(self, flaky) -> None:
        """The ndjson must expose it, or the report hides what was measured."""
        row = flaky.results[0].to_dict()
        assert row["success_per_repeat"] == [True, False, False]
        assert row["success_rate"] == pytest.approx(1 / 3)


class TestScoringIsMemoisedOnTheCrateHash:
    """Scoring every repeat must not multiply SHACL cost (#405).

    ``reaches_isa_tox_conformance`` runs a full 3-pass ``build_and_validate``, so a
    naive per-repeat call triples this harness's validation work at the default
    ``--repeats 3``. Two repeats with an identical canonical ``@graph`` cannot
    disagree about conformance, so the verdict is computed once per DISTINCT crate.
    """

    def test_a_deterministic_arm_validates_once_regardless_of_repeats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pipeline arm returns the same crate every time — one validation."""
        import eval.runner as runner_mod

        calls = {"n": 0}
        real = runner_mod.reaches_isa_tox_conformance

        def counting(state):
            calls["n"] += 1
            return real(state)

        monkeypatch.setattr(runner_mod, "reaches_isa_tox_conformance", counting)
        report = run_eval(_clean_factory, DEFAULT_CORPUS[:1], repeats=3)

        assert calls["n"] == 1, "identical crates were re-validated"
        # …and every repeat still carries a verdict.
        assert report.results[0].success_per_repeat == [True, True, True]

    def test_distinct_crates_are_each_validated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The memoisation must not collapse genuinely different crates."""
        import eval.runner as runner_mod

        calls = {"n": 0}
        real = runner_mod.reaches_isa_tox_conformance

        def counting(state):
            calls["n"] += 1
            return real(state)

        monkeypatch.setattr(runner_mod, "reaches_isa_tox_conformance", counting)
        _FlakyMockAgent.builds = 0
        report = run_eval(lambda: _FlakyMockAgent(), DEFAULT_CORPUS[:1], repeats=3)

        # Two distinct crates (the clean first build, then the empty ones).
        assert calls["n"] == 2
        assert report.results[0].success_per_repeat == [True, False, False]


class TestScoringIsMemoisedOnTheCrateHash:
    """Scoring every repeat must not multiply SHACL cost (#405).

    ``reaches_isa_tox_conformance`` runs a full 3-pass ``build_and_validate``, so a
    naive per-repeat call triples this harness's validation work at the default
    ``--repeats 3``. Two repeats with an identical canonical ``@graph`` cannot
    disagree about conformance, so the verdict is computed once per DISTINCT crate.
    """

    @staticmethod
    def _count_predicate(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        import eval.runner as runner_mod

        calls = {"n": 0}
        real = runner_mod.reaches_isa_tox_conformance

        def counting(state):
            calls["n"] += 1
            return real(state)

        monkeypatch.setattr(runner_mod, "reaches_isa_tox_conformance", counting)
        return calls

    def test_a_deterministic_arm_validates_once_regardless_of_repeats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pipeline arm returns the same crate every time — one validation."""
        calls = self._count_predicate(monkeypatch)
        report = run_eval(_clean_factory, DEFAULT_CORPUS[:1], repeats=3)

        assert calls["n"] == 1, "identical crates were re-validated"
        # …and every repeat still carries its own verdict.
        assert report.results[0].success_per_repeat == [True, True, True]

    def test_distinct_crates_are_each_validated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cache must not collapse a flaky case into a clean one."""
        calls = self._count_predicate(monkeypatch)
        _FlakyMockAgent.builds = 0
        report = run_eval(lambda: _FlakyMockAgent(), DEFAULT_CORPUS[:1], repeats=3)

        # Two distinct crates: the clean first build, then the empty ones.
        assert calls["n"] == 2
        assert report.results[0].success_per_repeat == [True, False, False]
