"""A property holding one value is written as that value, not a one-item array (RO-Crate 1.2).

RO-Crate 1.2 recommends the compact JSON-LD form, and roc-validator 0.13 reports every
one-item array as a SHOULD finding, which the gap engine then raised as "set this
field" on fields already set.
"""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import assemble_crate


def _ent(entity_id, type_, **fields):
    return Entity(entity_id, type_, fields=fields, _provenance=EntityProvenance(created_by="llm"))


def test_no_property_is_a_one_item_array() -> None:
    state = CrateState()
    state.metadata.title = "Uptake"
    state.add_entity(_ent("st1", "Study", name="Uptake study", creator="p1"))
    state.add_entity(_ent("p1", "Person", name="A. Researcher"))
    state.add_entity(_ent("assay_1", "Assay", name="Uptake assay"))
    state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2", accession="CVCL_0027"))
    state.add_entity(
        _ent(
            "prep",
            "LabProcess",
            name="Culture",
            process_type="TestSystemPreparation",
            assay_id="assay_1",
            cell_line=["cell_1"],
            culture_medium="DMEM",
        )
    )
    state.add_entity(_ent("chem_1", "MolecularEntity", name="Methimazole"))
    state.add_entity(
        _ent(
            "exp",
            "LabProcess",
            name="Exposure",
            process_type="Exposure",
            assay_id="assay_1",
            chemicals="chem_1",
            duration="24 h",
        )
    )
    graph = assemble_crate(state, output_dir=None, materialize_payload=False)
    doc = graph.metadata.generate()

    singletons = [
        (node.get("@id"), key)
        for node in doc["@graph"]
        for key, value in node.items()
        if not key.startswith("@") and isinstance(value, list) and len(value) == 1
    ]
    assert singletons == []
