"""Runner tests for the additive content-quality (entity-quota) metric.

The harness's success predicate stays unchanged (``{base, isa, tox}`` conformance).
On top of it, for cases that declare ``min_entities``, the runner now records a
per-case content-quality signal — ``entity_counts`` (what was drafted) and
``meets_quota`` (did the draft contain the demanded domain entities). This lets
the ReAct→pipeline A/B compare *draft content*, not just "did the agent act".

Fully offline via mock agents; ``build_and_validate`` runs, so the module carries
the harness 120s timeout.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState
from eval.agent_api import BuildOutcome
from eval.corpus import DEFAULT_CORPUS, EvalCase
from eval.runner import run_eval

pytestmark = pytest.mark.timeout(120)

_DRAFTING_CASE = next(c for c in DEFAULT_CORPUS if c.case_id == "structured-svhps22")


class _QuotaMeetingAgent:
    """Returns the case's own canned, quota-meeting state."""

    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id=None)


class _EmptyBackboneAgent:
    """Conformance-shaped state with NO domain content — misses the quota.

    Models the failure mode the new case is designed to catch: an agent that
    *acts* (reaches conformance) but drafts no real domain entities.
    """

    def build(self, case: EvalCase) -> BuildOutcome:
        return BuildOutcome(state=CrateState(), session_id=None)


class TestQuotaRecorded:
    def test_quota_meeting_build_records_meets_quota_true(self) -> None:
        report = run_eval(lambda: _QuotaMeetingAgent(), [_DRAFTING_CASE], repeats=1)
        res = report.results[0]
        assert res.success is True
        assert res.meets_quota is True
        assert res.entity_counts.get("MolecularEntity", 0) >= 1
        assert res.entity_counts.get("File", 0) >= 2

    def test_empty_backbone_misses_quota(self) -> None:
        report = run_eval(lambda: _EmptyBackboneAgent(), [_DRAFTING_CASE], repeats=1)
        res = report.results[0]
        # An empty crate cannot meet the domain-entity quota.
        assert res.meets_quota is False
        assert res.entity_counts.get("MolecularEntity", 0) == 0

    def test_case_without_quota_reports_meets_quota_none(self) -> None:
        # The pre-existing minimal case declares no min_entities, so its quality
        # is undefined (None) — the metric is purely additive.
        minimal = next(c for c in DEFAULT_CORPUS if c.case_id == "minimal-backbone")
        assert minimal.min_entities is None

        class _CleanAgent:
            def build(self, case: EvalCase) -> BuildOutcome:
                factory = case.build_state or CrateState
                return BuildOutcome(state=factory(), session_id=None)

        report = run_eval(lambda: _CleanAgent(), [minimal], repeats=1)
        res = report.results[0]
        assert res.meets_quota is None


class TestQuotaInSerializedReport:
    def test_to_dict_carries_the_quality_signal(self) -> None:
        report = run_eval(lambda: _QuotaMeetingAgent(), [_DRAFTING_CASE], repeats=1)
        row = report.results[0].to_dict()
        assert row["meets_quota"] is True
        assert "entity_counts" in row
        assert isinstance(row["entity_counts"], dict)
