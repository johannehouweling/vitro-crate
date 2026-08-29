"""Every tox process shape that declares `schema:object sh:minCount 1` must be
satisfiable by the build alone (2026-08-25 tox model/shape audit).

The shapes at profiles/shapes/tox/2_lab_process_cell_culture.ttl:34,
3_lab_process_exposure.ttl:41 and 4_lab_process_endpoint_readout.ttl:64 all
declare a REQUIRED schema:object. A build that can emit a process with none of
them produces a crate that fails the profile the tool asserts end to end.

Each test perturbs a minimal state one way and asserts the tox verdict, so a
failure names the perturbation rather than the fixture.
"""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.validation import build_and_validate


def _state_with(entity_id: str, fields: dict) -> CrateState:
    """A CrateState holding exactly one LabProcess, with nothing else to link to."""
    state = CrateState()
    state.metadata.title = "Floor probe"
    state.add_entity(
        Entity(
            entity_id=entity_id,
            type="LabProcess",
            fields=fields,
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


def _tox_issues(state: CrateState, prop: str) -> list[dict]:
    """REQUIRED tox issues on *prop*, e.g. 'http://schema.org/object'."""
    result = build_and_validate(state, severity="required", profile="tox")
    return [i for i in result["issues"] if i.get("property") == prop]


class TestExposureInputFloor:
    """An Exposure with no cell culture still declares what it exposed (#650)."""

    def test_exposure_with_no_resolvable_cells_still_has_an_object(self):
        state = _state_with(
            "proc_exp",
            {"process_type": "Exposure", "name": "Amiodarone exposure"},
        )

        assert _tox_issues(state, "http://schema.org/object") == []

    def test_the_synthesized_input_is_a_sample_not_an_empty_list(self):
        """The floor must add a node, not merely silence the count.

        Guards the lazy fix: emitting `"input": [""]` or dropping the key would
        also clear the minCount in some validators but says nothing about what
        was exposed.
        """
        from builder.tools.builder import assemble_crate

        state = _state_with(
            "proc_exp",
            {"process_type": "Exposure", "name": "Amiodarone exposure"},
        )
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
        node = next(
            e
            for e in crate.get_entities()
            if e.properties().get("additionalType") == "Exposure"
        )
        objects = node.properties().get("input")
        assert objects, "Exposure emitted no schema:object at all"
        assert not isinstance(objects, str)
