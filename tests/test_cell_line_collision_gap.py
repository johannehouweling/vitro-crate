"""Two unresolved cell lines whose names differ only in punctuation (#678, inv. 6).

S-VHPS22 carries "H4" and "H-4" as two CellLineSample entities, neither with an
accession. They are almost certainly one line typed twice — and the builder must
NOT act on "almost certainly". Cellosaurus registers H4 (CVCL_1239) and H-4
(CVCL_6C19) as distinct records that each list the other's name as a synonym, and
a third (CVCL_HA56) also answers to H4; all three are human. No general
discriminator separates "one line typed twice" from "two lines sharing a
synonym", so merging on normalised name would fabricate an identity claim.

The collision is reported instead, for the depositor to settle.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.gap_analysis import assess_gaps

pytestmark = pytest.mark.timeout(180)


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _state(*lines):
    state = CrateState()
    state.add_entity(_ent("study_1", "Study", name="S"))
    for eid, fields in lines:
        state.add_entity(_ent(eid, "CellLineSample", **fields))
    return state


def _collisions(state):
    return [g for g in assess_gaps(state).gaps if g.source == "identity"]


class TestTheCollisionIsReported:
    def test_two_unresolved_near_identical_names_collide(self):
        gaps = _collisions(
            _state(("cell_h4", {"name": "H4"}), ("cell_h_4", {"name": "H-4"}))
        )
        assert len(gaps) == 1, f"expected one collision gap, got {gaps}"
        message = gaps[0].message
        assert "H4" in message and "H-4" in message, message

    def test_the_gap_does_not_assert_they_are_the_same(self):
        """The whole point. It asks; it must not conclude."""
        gap = _collisions(
            _state(("cell_h4", {"name": "H4"}), ("cell_h_4", {"name": "H-4"}))
        )[0]
        assert gap.tier == "SHOULD", "a question for the depositor is not a blocker"
        assert gap.fix_hint == "ask-user"
        assert gap.auto_fixable is False, (
            "nothing here is resolvable from state alone — that is the finding"
        )

    def test_it_is_reported_once_per_group_not_once_per_entity(self):
        gaps = _collisions(
            _state(
                ("cell_h4", {"name": "H4"}),
                ("cell_h_4", {"name": "H-4"}),
                ("cell_h4b", {"name": "h 4"}),
            )
        )
        assert len(gaps) == 1, f"three spellings of one name is one question: {gaps}"


class TestWhatMustNotCollide:
    def test_resolved_lines_never_collide(self):
        """Two accessions is two lines, whatever the names look like.

        This is the case that makes punctuation-merging unsafe: Cellosaurus
        really does register H4 and H-4 separately.
        """
        gaps = _collisions(
            _state(
                ("cell_h4", {"name": "H4", "accession": "CVCL_1239"}),
                ("cell_h_4", {"name": "H-4", "accession": "CVCL_6C19"}),
            )
        )
        assert not gaps, f"resolved lines are settled, not colliding: {gaps}"

    def test_one_resolved_one_not_does_not_collide(self):
        """An accession on either side answers the question already."""
        gaps = _collisions(
            _state(
                ("cell_h4", {"name": "H4", "accession": "CVCL_1239"}),
                ("cell_h_4", {"name": "H-4"}),
            )
        )
        assert not gaps, gaps

    def test_genuinely_different_names_do_not_collide(self):
        gaps = _collisions(
            _state(("cell_a", {"name": "SK-N-AS"}), ("cell_b", {"name": "MO3.13"}))
        )
        assert not gaps, gaps

    def test_a_single_unresolved_line_does_not_collide(self):
        assert not _collisions(_state(("cell_h4", {"name": "H4"})))

    def test_a_derivative_is_not_a_collision(self):
        """`cell_line_names_match`'s rule, kept: CHO-K1 hOATP1C1 is not CHO-K1.

        Punctuation-insensitivity must not become token-insensitivity, or an
        engineered derivative reads as a duplicate of its parent.
        """
        gaps = _collisions(
            _state(
                ("cell_p", {"name": "CHO-K1"}),
                ("cell_d", {"name": "CHO-K1 hOATP1C1"}),
            )
        )
        assert not gaps, f"a derivative is a different line: {gaps}"
