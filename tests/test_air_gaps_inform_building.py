"""The AI-readiness axis has to change crates, not just grade them.

MIT and FAIR both reach the builder through ``gap_analysis`` → ``Gap`` →
``run_guidance``, so a low score makes the builder ask for the missing value. The
checklist this axis replaced reached exactly one KPI tile and nothing else. These
tests hold the new axis to the same contract as the other two — and to the limits
the guidance loop actually has, which is the more important half: an axis that
claims actionability the loop cannot deliver burns a human turn on an answer the
loop then discards.
"""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.gap_analysis import REPORT_ONLY, Gap, _air_gaps, _sort_key, assess_gaps


def _graph(*nodes: dict) -> dict:
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": "A crate"},
            *nodes,
        ]
    }


def _state_with_one_protocol() -> CrateState:
    state = CrateState()
    protocol = Entity(
        entity_id="prot_001",
        type="LabProtocol",
        fields={"name": "Exposure protocol"},
        _provenance=EntityProvenance(created_by="llm"),
    )
    state.add_entity(protocol)
    return state


class TestAFailingCriterionBecomesAGap:
    def test_it_is_sourced_to_the_air_axis(self):
        gaps, _ = _air_gaps(CrateState(), graph=_graph())
        assert gaps
        assert {g.source for g in gaps} == {"air"}

    def test_the_message_carries_the_published_practice_text(self):
        gaps, _ = _air_gaps(CrateState(), graph=_graph())
        payload_integrity = next(g for g in gaps if "3.c" in (g.property or "") + g.message)
        assert "cryptographic hash" in payload_integrity.message

    def test_the_message_is_stable_across_runs(self):
        """`_gap_identity` is (source, entity_id, property, message), and the loop's
        skip set depends on it. A live count in the message would change the identity
        whenever the crate changed, re-drawing a gap the user already answered."""
        first, _ = _air_gaps(CrateState(), graph=_graph())
        second, _ = _air_gaps(CrateState(), graph=_graph({"@id": "x.csv", "@type": "File"}))
        shared = {g.property: g.message for g in first} | {}
        for gap in second:
            if gap.property in shared:
                assert gap.message == shared[gap.property]

    def test_an_unassessable_criterion_never_becomes_a_gap(self):
        """"We cannot see it" is not "you failed to do it"."""
        gaps, _ = _air_gaps(CrateState(), graph=_graph())
        ethics = [g for g in gaps if (g.property or "").startswith(("4.a", "4.b", "4.c"))]
        assert not ethics

    def test_the_summary_reports_the_profile_and_no_aggregate(self):
        _, summary = _air_gaps(CrateState(), graph=_graph())
        assert len(summary["dimensions"]) == 7
        assert "score" not in summary and "overall" not in summary


class TestTheLoopHasTheFinalVeto:
    def test_no_air_gap_is_ever_must(self):
        """MUST is the SHACL build gate. No RO-Crate profile requires AI-readiness,
        so emitting one would assert a conformance failure that is not one."""
        gaps, _ = _air_gaps(CrateState(), graph=_graph())
        assert all(g.tier in ("SHOULD", "MAY") for g in gaps)

    def test_no_air_gap_names_an_identifier_field(self):
        from builder.tools.field_kinds import is_identifier_field

        gaps, _ = _air_gaps(CrateState(), graph=_graph())
        for gap in gaps:
            if gap.fix_hint != REPORT_ONLY:
                assert not is_identifier_field(gap.property or "")

    def test_a_declared_remedy_with_no_target_is_forced_report_only(self):
        """3.a routes to `LabProtocol:description`. With no LabProtocol in state
        there is nothing to write to, and the YAML's intent must not survive that."""
        gaps, _ = _air_gaps(CrateState(), graph=_graph())
        documentation = next(g for g in gaps if (g.property or "").startswith("3.a"))
        assert documentation.fix_hint == REPORT_ONLY

    def test_the_same_remedy_becomes_actionable_once_the_target_exists(self):
        state = _state_with_one_protocol()
        gaps, _ = _air_gaps(state, graph=_graph())
        documentation = next(g for g in gaps if g.entity_type == "LabProtocol")
        assert documentation.fix_hint != REPORT_ONLY
        assert documentation.property == "description"

    def test_nothing_is_marked_auto_fixable(self):
        """`auto_fixable` means precisely "fix_required_issues can clear it"."""
        gaps, _ = _air_gaps(_state_with_one_protocol(), graph=_graph())
        assert not any(g.auto_fixable for g in gaps)


class TestItReachesTheGuidanceLoop:
    def test_an_actionable_air_gap_survives_the_report_only_filter(self):
        from builder.agents.pipeline.guidance import _next_actionable_gap
        from builder.tools.gap_analysis import GapReport

        gaps, _ = _air_gaps(_state_with_one_protocol(), graph=_graph())
        actionable = [g for g in gaps if g.fix_hint != REPORT_ONLY]
        assert actionable, "the axis informs building at zero points"
        report = GapReport(gaps=sorted(actionable, key=_sort_key))
        assert _next_actionable_gap(report, skipped=set()) is not None

    def test_report_only_air_gaps_never_precede_committable_ones(self):
        committable = Gap("SHOULD", "air", None, "LabProtocol", "description", "m", None, "ask-user", False)
        reported = Gap("SHOULD", "air", None, None, "4.d", "m2", None, REPORT_ONLY, False)
        assert sorted([reported, committable], key=_sort_key)[0] is committable


class TestItJoinsTheUnifiedReport:
    def test_assess_gaps_carries_an_air_summary(self):
        report = assess_gaps(CrateState())
        assert len(report.air_summary.get("dimensions", [])) == 7

    def test_air_gaps_are_counted_like_every_other_source(self):
        """A count that silently excludes one source is a number nobody can read."""
        report = assess_gaps(CrateState())
        air = [g for g in report.gaps if g.source == "air"]
        assert air
        assert report.counts["should_open"] + report.counts["may_open"] >= len(air)
