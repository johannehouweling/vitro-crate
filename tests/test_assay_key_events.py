"""An assay can measure several Key Events, and naming one adds it.

``link_assay_to_key_event`` stores the event a name matches among those the crate
holds, and never one built from the name: which Key Event an assay measures is a
scientific claim, so a name matching none writes nothing (D5).
"""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance


class TestAnAssayCanMeasureSeveralKeyEvents:
    """One experiment can report two events — a viability control beside the
    inhibition it is built to detect — so an answer naming several is a normal
    answer, not a malformed one.
    """

    def _crate(self) -> CrateState:
        state = CrateState()
        assay = Entity(
            entity_id="assay_1", type="Assay", _provenance=EntityProvenance(created_by="llm")
        )
        assay.set_fields_from_dict({"name": "An assay"}, source="llm")
        state.add_entity(assay)
        for eid, name in (
            ("KeyEvent:ke1", "Inhibition, OATP1C1"),
            ("KeyEvent:ke2", "Decreased cell viability"),
        ):
            event = Entity(
                entity_id=eid, type="KeyEvent", _provenance=EntityProvenance(created_by="lookup")
            )
            event.set_fields_from_dict({"name": name}, source="lookup")
            state.add_entity(event)
        return state

    def test_two_events_link_in_one_call(self):
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        result = link_assay_to_key_event(
            state, "assay_1", ["Inhibition, OATP1C1", "Decreased cell viability"]
        )
        assert result["ok"] is True
        assert result["key_event_ids"] == ["KeyEvent:ke1", "KeyEvent:ke2"]

    def test_a_second_answer_adds_rather_than_replaces(self):
        """Naming one event later is not a claim the earlier one was wrong."""
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        link_assay_to_key_event(state, "assay_1", "Inhibition, OATP1C1")
        result = link_assay_to_key_event(state, "assay_1", "Decreased cell viability")
        assert result["key_event_ids"] == ["KeyEvent:ke1", "KeyEvent:ke2"]

    def test_the_same_event_twice_is_recorded_once(self):
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        link_assay_to_key_event(state, "assay_1", "Inhibition, OATP1C1")
        result = link_assay_to_key_event(state, "assay_1", "Inhibition, OATP1C1")
        assert result["key_event_ids"] == ["KeyEvent:ke1"]

    def test_a_good_name_survives_a_bad_one(self):
        """Refusing everything over one typo throws away correct science."""
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        result = link_assay_to_key_event(
            state, "assay_1", ["Inhibition, OATP1C1", "Not a real event"]
        )
        assert result["ok"] is True
        assert result["key_event_ids"] == ["KeyEvent:ke1"]
        assert [u["name"] for u in result["unmatched"]] == ["Not a real event"]

    def test_no_name_matching_writes_nothing(self):
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        result = link_assay_to_key_event(state, "assay_1", ["Nope", "Also nope"])
        assert result["ok"] is False
        assay = state.get_entity("assay_1")
        assert assay is not None
        assert assay.fields.get("keyEvent") is None

    def test_the_single_name_form_is_unchanged(self):
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        result = link_assay_to_key_event(state, "assay_1", "Inhibition, OATP1C1")
        assert result["key_event_id"] == "KeyEvent:ke1"
        assert result["matched_name"] == "Inhibition, OATP1C1"
