"""A missing entity id is answered with the ids it was probably reaching for.

Ids are minted by the drafting tools from an entity's name, so an agent that
rebuilds one from the name it remembers gets close but not exact —
``assay_inhibition_of_oatp1c1_mediated_cellular_uptake_of_thyroxine`` for an
assay actually stored under a shorter id, ``org_erasmus_mc`` for
``org_erasmus_medical_center``.

``entity_not_found_message`` was written for exactly this and says "Did you
mean: …". It was wired into ``set_fields`` and nowhere else, so the three tools
that fail this way in practice — ``link``, ``attach_files`` and
``draft_process_chain`` — still answered with a flat "not found". A profiled
session lost four iterations to it: each failure told the agent its guess was
wrong without telling it what was right, so it guessed again.

These tests pin every entry point to the shared message, so the next tool that
grows an id argument is the only one that can regress.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance


def _state(*ids: tuple[str, str]) -> CrateState:
    state = CrateState()
    for entity_id, entity_type in ids:
        entity = Entity(
            entity_id=entity_id,
            type=entity_type,  # ty: ignore[invalid-argument-type]
            _provenance=EntityProvenance(created_by="llm"),
        )
        entity.set_fields_from_dict({"name": entity_id}, source="llm")
        state.add_entity(entity)
    return state


class TestTheNearMissIsNamed:
    def test_link_names_the_id_it_meant(self):
        """The session's actual failure: org_erasmus_mc."""
        from builder.tools.provenance import link

        state = _state(("org_erasmus_medical_center", "Organization"), ("proc_a", "LabProcess"))
        with pytest.raises(ValueError, match="org_erasmus_medical_center"):
            link(state, "proc_a", "input", "org_erasmus_mc")

    def test_link_checks_its_source_too(self):
        from builder.tools.provenance import link

        state = _state(("proc_cell_culture", "LabProcess"), ("sample_a", "Sample"))
        with pytest.raises(ValueError, match="proc_cell_culture"):
            link(state, "proc_cellculture", "result", "sample_a")

    def test_attach_files_names_the_id_it_meant(self):
        from builder.tools.provenance import attach_files

        state = _state(("study_oatp1c1_uptake", "Study"))
        with pytest.raises(ValueError, match="study_oatp1c1_uptake"):
            attach_files(state, to="study_oatp1c1_inhibition")

    def test_draft_process_chain_names_the_id_it_meant(self):
        from builder.tools.composites import draft_process_chain

        state = _state(("assay_oatp1c1_uptake_inhibition", "Assay"))
        with pytest.raises(ValueError, match="assay_oatp1c1_uptake_inhibition"):
            draft_process_chain(state, assay_id="assay_oatp1c1_uptake_inhibiton", chain=[])


class TestWithNoNearMissItStillHelps:
    def test_it_lists_real_ids_rather_than_dead_ending(self):
        """Naming a few real ids beats "no" when nothing is close."""
        from builder.tools.provenance import link

        state = _state(("sample_alpha", "Sample"), ("proc_a", "LabProcess"))
        with pytest.raises(ValueError, match="Existing ids include"):
            link(state, "proc_a", "input", "zzzzzzzzzzzz")

    def test_an_empty_crate_says_so(self):
        from builder.tools.provenance import attach_files

        with pytest.raises(ValueError, match="no entities yet"):
            attach_files(CrateState(), to="study_1")


class TestTheMessageStaysUseful:
    def test_it_names_the_tool_that_failed(self):
        """Three tools share one message; the caller must stay identifiable."""
        from builder.tools.provenance import attach_files

        state = _state(("study_a", "Study"))
        with pytest.raises(ValueError, match="attach_files target"):
            attach_files(state, to="study_b")

    def test_it_explains_where_ids_come_from(self):
        """The fix is to reuse the drafter's id, not to guess a better one."""
        from builder.tools.provenance import link

        state = _state(("sample_alpha", "Sample"), ("proc_a", "LabProcess"))
        with pytest.raises(ValueError, match="minted by the drafting tools"):
            link(state, "proc_a", "input", "sample_alfa")
