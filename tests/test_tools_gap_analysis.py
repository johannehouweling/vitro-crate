"""Tests for builder/tools/gap_analysis.py — the gap engine (#179, Stage C).

``assess_gaps`` unifies the three assessors — the in-memory SHACL validation
(``build_and_validate``), the MIT coverage report (``assess_mit_coverage``), and
the FAIR/DSM assessment (``assess_fair_maturity``) — into ONE prioritized
``GapReport`` the guidance agent consumes. It is a *pure, deterministic* library
function: no LLM, no network, idempotent.

These tests are validation-heavy (they run the owlrl-heavy SHACL validator one or
more times), so they carry the 120s marker like ``tests/test_tools_repair.py``;
CI's suite-wide ``--timeout=30`` is overridden per-module.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.gap_analysis import (
    _CRATE_SETTABLE_FIELDS,
    Gap,
    GapReport,
    _local_name,
    assess_gaps,
)

pytestmark = pytest.mark.timeout(120)


def _local(prop: str | None) -> str:
    """Local name of a property IRI (test-side mirror of the engine helper)."""
    return _local_name(prop)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_tools_repair.py helpers)
# ---------------------------------------------------------------------------


def _entity(entity_id: str, type_: EntityType, **fields) -> Entity:
    e = Entity(
        entity_id=entity_id,
        type=type_,
        fields=dict(fields),
        _provenance=EntityProvenance(created_by="llm"),
    )
    for key in fields:
        e.set_field_status(key, "filled", "llm")
    return e


def _backbone() -> CrateState:
    """A BASE/ISA/TOX-passing Investigation -> Study -> Assay backbone."""
    state = CrateState()
    state.metadata.title = "Gap test crate"
    state.add_entity(
        _entity("inv1", "Investigation", name="Inv", description="d", identifier="INV-1")
    )
    state.add_entity(
        _entity("st1", "Study", name="St", description="d", investigation_id="inv1")
    )
    state.add_entity(_entity("as1", "Assay", name="As", description="d", study_id="st1"))
    return state


def _endpoint_readout_missing_result(n_files: int = 1) -> CrateState:
    """Backbone + an EndpointReadout with no result + ``n_files`` File entities.

    The TOX MUST issue 'EndpointReadout MUST have a result' is auto-fixable iff
    exactly one un-wired File exists (the deterministic-repair rule's predicate).
    """
    state = _backbone()
    state.add_entity(
        _entity(
            "er1",
            "LabProcess",
            process_type="EndpointReadout",
            name="Readout",
            assay_id="as1",
        )
    )
    for i in range(n_files):
        state.add_entity(
            _entity(f"f{i}", "File", name=f"raw{i}.csv", dest_path=f"data/raw{i}.csv")
        )
    return state


# ---------------------------------------------------------------------------
# Return shape
# ---------------------------------------------------------------------------


class TestReturnShape:
    def test_returns_gap_report(self):
        report = assess_gaps(CrateState())
        assert isinstance(report, GapReport)
        assert isinstance(report.gaps, list)
        assert isinstance(report.conformance, dict)
        assert isinstance(report.mit_overall, float)
        assert isinstance(report.fair_summary, dict)
        assert isinstance(report.counts, dict)

    def test_gaps_are_gap_instances(self):
        report = assess_gaps(_backbone())
        assert all(isinstance(g, Gap) for g in report.gaps)

    def test_counts_keys(self):
        report = assess_gaps(_backbone())
        assert set(report.counts) == {"must_open", "should_open", "may_open"}

    def test_conformance_has_three_layers(self):
        report = assess_gaps(_backbone())
        assert set(report.conformance) == {"base", "isa", "tox"}

    def test_fair_summary_shape(self):
        report = assess_gaps(_backbone())
        assert "dsm_level" in report.fair_summary
        assert "indicators_passed" in report.fair_summary
        assert "indicators_failed" in report.fair_summary


# ---------------------------------------------------------------------------
# MUST gaps present on an empty/backbone-only crate, sorted MUST-first
# ---------------------------------------------------------------------------


class TestMustGapsSortedFirst:
    def test_empty_crate_has_must_gaps(self):
        report = assess_gaps(CrateState())
        must = [g for g in report.gaps if g.tier == "MUST"]
        assert must, "an empty crate must surface at least one MUST gap"
        # Every MUST gap comes from a SHACL required issue.
        assert all(g.source == "shacl" for g in must)
        assert report.counts["must_open"] == len(must)

    def test_gaps_sorted_must_then_should_then_may(self):
        report = assess_gaps(CrateState())
        order = {"MUST": 0, "SHOULD": 1, "MAY": 2}
        ranks = [order[g.tier] for g in report.gaps]
        assert ranks == sorted(ranks), "gaps must be sorted MUST -> SHOULD -> MAY"

    def test_empty_crate_surfaces_identifier_must(self):
        report = assess_gaps(CrateState())
        must = [g for g in report.gaps if g.tier == "MUST"]
        assert any(
            (g.property or "").endswith("identifier") for g in must
        ), [g.property for g in must]


# ---------------------------------------------------------------------------
# A crate that passes MUST but misses SHOULD/MAY -> no MUST gaps
# ---------------------------------------------------------------------------


class TestPassesMustMissesShould:
    def test_backbone_has_no_must_gaps(self):
        report = assess_gaps(_backbone())
        assert all(g.tier != "MUST" for g in report.gaps), [
            (g.tier, g.source, g.message) for g in report.gaps if g.tier == "MUST"
        ]
        assert report.counts["must_open"] == 0

    def test_backbone_has_should_or_may_gaps(self):
        report = assess_gaps(_backbone())
        # The backbone passes all REQUIRED checks but is far from complete: MIT
        # core params and SHACL SHOULD recommendations are open.
        assert any(g.tier in ("SHOULD", "MAY") for g in report.gaps)

    def test_backbone_conformance_all_true(self):
        report = assess_gaps(_backbone())
        assert report.conformance == {"base": True, "isa": True, "tox": True}


# ---------------------------------------------------------------------------
# Source coverage — the three assessors are all represented
# ---------------------------------------------------------------------------


class TestSourcesUnified:
    def test_mit_gaps_present(self):
        report = assess_gaps(_backbone())
        mit = [g for g in report.gaps if g.source == "mit"]
        assert mit, "unfilled MIT params must surface as gaps"
        # MIT gaps are SHOULD (core) or MAY (additional), never MUST.
        assert all(g.tier in ("SHOULD", "MAY") for g in mit)

    def test_fair_gaps_present(self):
        report = assess_gaps(_backbone())
        fair = [g for g in report.gaps if g.source == "fair"]
        assert fair, "failing FAIR indicators must surface as gaps"
        assert all(g.tier in ("SHOULD", "MAY") for g in fair)

    def test_mit_gap_carries_property_and_suggestion(self):
        report = assess_gaps(_backbone())
        mit = [g for g in report.gaps if g.source == "mit"]
        # crate_slot -> property, description/standards -> suggestion.
        assert any(g.property for g in mit)
        assert any(g.suggestion for g in mit)
        # MIT gaps are filled by the user / a draft, not deterministic repair.
        assert all(g.auto_fixable is False for g in mit)
        # A MIT gap is either committable (ask-user / draft, when its field maps
        # to a deterministic crate-level target) or report-only (no settable
        # target from the gap's own fields). Never anything else.
        assert all(g.fix_hint in ("ask-user", "draft", "report-only") for g in mit)


# ---------------------------------------------------------------------------
# report-only fix_hint — uncommittable gaps are surfaced, never asked
# ---------------------------------------------------------------------------


class TestReportOnlyGaps:
    def test_fair_gaps_are_report_only(self):
        """FAIR gaps (entity_id None, property=indicator-id) cannot be committed.

        Their ``property`` is an indicator token (e.g. ``RDA-F1-02M``) that maps
        to no settable crate field, so the guidance loop must never spend an
        ask-user turn on them — they are ``report-only``.
        """
        report = assess_gaps(_backbone())
        fair = [g for g in report.gaps if g.source == "fair"]
        assert fair, "failing FAIR indicators must surface as gaps"
        assert all(g.fix_hint == "report-only" for g in fair), [
            (g.property, g.fix_hint) for g in fair
        ]

    def test_crate_level_mit_without_settable_target_is_report_only(self):
        """A crate-level (entity_id None) MIT gap whose field maps to no crate
        metadata slot has no deterministic target -> report-only."""
        report = assess_gaps(_backbone())
        # ``char`` / ``keywords`` / ``datePublished`` style MIT fields are not in
        # the crate-metadata field set, so with entity_id None they are report-only.
        report_only_mit = [
            g
            for g in report.gaps
            if g.source == "mit"
            and g.entity_id is None
            and _local(g.property) not in _CRATE_SETTABLE_FIELDS
        ]
        assert report_only_mit, "expected at least one uncommittable MIT gap"
        assert all(g.fix_hint == "report-only" for g in report_only_mit), [
            (g.property, g.fix_hint) for g in report_only_mit
        ]

    def test_committable_mit_keeps_ask_or_draft(self):
        """A crate-level MIT gap whose field DOES map to a crate slot
        (name / description / identifier) is still committable -> ask-user/draft."""
        report = assess_gaps(_backbone())
        committable_mit = [
            g
            for g in report.gaps
            if g.source == "mit"
            and g.entity_id is None
            and _local(g.property) in _CRATE_SETTABLE_FIELDS
        ]
        assert committable_mit, "expected at least one committable MIT gap"
        assert all(g.fix_hint in ("ask-user", "draft") for g in committable_mit), [
            (g.property, g.fix_hint) for g in committable_mit
        ]

    def test_committable_gaps_sort_before_report_only_within_tier(self):
        """Within a tier, gaps the loop can act on must precede report-only ones."""
        report = assess_gaps(_backbone())
        for tier in ("SHOULD", "MAY"):
            within = [g for g in report.gaps if g.tier == tier]
            committable_flags = [g.fix_hint != "report-only" for g in within]
            # All committable (True) gaps must come before report-only (False):
            # the boolean sequence is non-increasing.
            assert committable_flags == sorted(committable_flags, reverse=True), [
                (g.fix_hint, g.source, g.property) for g in within
            ]


# ---------------------------------------------------------------------------
# auto_fixable — deterministic vs ask-user
# ---------------------------------------------------------------------------


class TestAutoFixable:
    def test_deterministically_repairable_must_is_auto_fixable(self):
        """A missing EndpointReadout result with ONE in-state File is auto-fixable."""
        report = assess_gaps(_endpoint_readout_missing_result(n_files=1))
        result_gaps = [
            g
            for g in report.gaps
            if g.tier == "MUST" and (g.property or "").endswith("result")
        ]
        assert result_gaps, "the missing-result MUST gap should be present"
        assert all(g.auto_fixable for g in result_gaps), [
            (g.message, g.auto_fixable) for g in result_gaps
        ]
        assert all(g.fix_hint == "fix_required_issues" for g in result_gaps)

    def test_ambiguous_missing_result_is_not_auto_fixable(self):
        """TWO candidate Files == ambiguous: the repair loop declines -> ask-user."""
        report = assess_gaps(_endpoint_readout_missing_result(n_files=2))
        result_gaps = [
            g
            for g in report.gaps
            if g.tier == "MUST" and (g.property or "").endswith("result")
        ]
        assert result_gaps
        assert all(g.auto_fixable is False for g in result_gaps)
        assert all(g.fix_hint == "ask-user" for g in result_gaps)

    def test_no_file_missing_result_is_not_auto_fixable(self):
        """No File in state == needs NEW content (D5): not auto-fixable."""
        report = assess_gaps(_endpoint_readout_missing_result(n_files=0))
        result_gaps = [
            g
            for g in report.gaps
            if g.tier == "MUST" and (g.property or "").endswith("result")
        ]
        assert result_gaps
        assert all(g.auto_fixable is False for g in result_gaps)

    def test_non_repairable_must_is_not_auto_fixable(self):
        """The empty-crate identifier MUST has no repair rule -> ask-user."""
        report = assess_gaps(CrateState())
        ident = [
            g
            for g in report.gaps
            if g.tier == "MUST" and (g.property or "").endswith("identifier")
        ]
        assert ident
        assert all(g.auto_fixable is False for g in ident)
        assert all(g.fix_hint == "ask-user" for g in ident)


# ---------------------------------------------------------------------------
# Determinism / idempotence
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_calls_identical_ordered_output(self):
        state = _endpoint_readout_missing_result(n_files=1)
        first = assess_gaps(state)
        second = assess_gaps(state)

        def key(g: Gap) -> tuple:
            return (
                g.tier,
                g.source,
                g.entity_id,
                g.property,
                g.message,
                g.suggestion,
                g.fix_hint,
                g.auto_fixable,
            )

        assert [key(g) for g in first.gaps] == [key(g) for g in second.gaps]
        assert first.counts == second.counts
        assert first.conformance == second.conformance

    def test_does_not_mutate_state(self):
        state = _endpoint_readout_missing_result(n_files=1)
        snapshot = state.to_json()
        assess_gaps(state)
        assert state.to_json() == snapshot, "assess_gaps must be side-effect-free"


# ---------------------------------------------------------------------------
# Stable secondary ordering within a tier
# ---------------------------------------------------------------------------


class TestStableSecondaryOrder:
    def test_within_tier_sorted_by_source_entity_property(self):
        """Within a tier: committable gaps first, then report-only ones, and each
        of those two groups is stably sorted by (source, entity_id, property)."""
        report = assess_gaps(_backbone())
        for tier in ("MUST", "SHOULD", "MAY"):
            within = [g for g in report.gaps if g.tier == tier]
            committable = [g for g in within if g.fix_hint != "report-only"]
            report_only = [g for g in within if g.fix_hint == "report-only"]

            def _keys(gaps):
                return [(g.source, g.entity_id or "", g.property or "") for g in gaps]

            assert _keys(committable) == sorted(_keys(committable)), (
                f"{tier} committable gaps must have a stable secondary order"
            )
            assert _keys(report_only) == sorted(_keys(report_only)), (
                f"{tier} report-only gaps must have a stable secondary order"
            )
