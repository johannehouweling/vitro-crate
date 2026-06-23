"""Tests for the full ISA-Tox domain-model mapping in build_crate.

These pin the contract from profiles/docs/isa_tox.md: Study/Assay as
Dataset+additionalType linked by hasPart, LabProcess subtypes discriminated by
additionalType with executesLabProtocol and input/output (→ schema:object/result
via the context), protocol synthesis, Exposure object carrying the compounds,
conformsTo gating on validation, and the #-fragment / resolvable-URI id scheme.
"""

from __future__ import annotations

import json

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _build(state, tmp_path):
    out = tmp_path / "crate"
    result = build_crate(state, str(out))
    assert result["success"] is True, result
    with open(out / "ro-crate-metadata.json") as f:
        graph = json.load(f)["@graph"]
    return graph, {e["@id"]: e for e in graph}


def _ids(value):
    """Normalize a hasPart/about/object value to a list of @id strings."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [v.get("@id") if isinstance(v, dict) else v for v in items]


class TestStructuralDatasets:
    def test_study_and_assay_are_datasets_with_additionaltype(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="My Study"))
        state.add_entity(_ent("assay_1", "Assay", name="My Assay", study_id="study_1"))
        _, by_id = _build(state, tmp_path)

        assert by_id["#Study_study_1"]["@type"] == "Dataset"
        assert by_id["#Study_study_1"]["additionalType"] == "Study"
        assert by_id["#Assay_assay_1"]["@type"] == "Dataset"
        assert by_id["#Assay_assay_1"]["additionalType"] == "Assay"

    def test_haspart_wiring(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="S"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        _, by_id = _build(state, tmp_path)

        # root contains the study; study contains the assay
        assert "#Study_study_1" in _ids(by_id["./"].get("hasPart"))
        assert "#Assay_assay_1" in _ids(by_id["#Study_study_1"].get("hasPart"))


class TestLabProcessSubtypes:
    def _state_with_process(self, process_type, **proc_fields):
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        state.add_entity(
            _ent(
                "proc_1",
                "LabProcess",
                name=process_type,
                process_type=process_type,
                assay_id="assay_1",
                **proc_fields,
            )
        )
        return state

    def test_cell_culture(self, tmp_path):
        state = self._state_with_process(
            "CellCulture",
            cell_line="cell_1",
            culture_medium="DMEM",
            result="sample_out",
        )
        state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2", accession="CVCL_0027"))
        state.add_entity(_ent("sample_out", "Sample", name="cultured"))
        _, by_id = _build(state, tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        assert proc["additionalType"] == "CellCulture"
        assert proc.get("executesLabProtocol")  # SHOULD, always synthesized
        # input → schema:object, output → schema:result (via the @context)
        assert "#CellLineSample_cell_1" in _ids(proc.get("input"))
        assert "#Sample_sample_out" in _ids(proc.get("output"))

    def test_exposure_object_is_cells_result_is_condition_table(self, tmp_path):
        # ISA forbids a MolecularEntity as a process object (objects MUST be
        # File/Sample/BioSample). The compound is therefore NOT in `object`; the
        # Exposure's result is the CSVW condition table, which links the compound.
        state = self._state_with_process(
            "Exposure",
            samples="sample_cult",
            chemicals="chem_1",
            duration="24h",
            microplate="96-well",
        )
        state.add_entity(_ent("sample_cult", "Sample", name="cultured"))
        state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin A"))
        _, by_id = _build(state, tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        assert proc["additionalType"] == "Exposure"

        obj_ids = _ids(proc.get("input"))  # input → schema:object
        assert "#Sample_sample_cult" in obj_ids  # the cells (Sample) — allowed
        assert "#MolecularEntity_chem_1" not in obj_ids  # MolecularEntity — ISA-forbidden

        result_ids = _ids(proc.get("output"))  # output → schema:result (MUST)
        assert result_ids, "Exposure MUST emit a result (the condition table)"
        table = by_id[result_ids[0]]
        # the result is the condition table: a File (ISA-valid result) + csvw:Table
        tt = table["@type"] if isinstance(table["@type"], list) else [table["@type"]]
        assert "File" in tt and "csvw:Table" in tt
        # the compound is connected to the Exposure THROUGH the condition table
        assert "#MolecularEntity_chem_1" in _ids(table.get("about"))

    def test_exposure_condition_table_is_typed_csvw(self, tmp_path):
        # Issue #94: the condition table must be typed CSVW — a linked schema
        # entity with per-column datatype + propertyUrl, the cell-line/compound
        # columns resolving to their entity ids (valueUrl).
        state = self._state_with_process(
            "Exposure",
            samples="sample_cult",
            chemicals="chem_1",
            duration="24h",
        )
        state.add_entity(_ent("sample_cult", "Sample", name="cultured"))
        state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin A"))
        _, by_id = _build(state, tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        table = by_id[_ids(proc.get("output"))[0]]

        # the table is linked to a schema entity via conformsTo (not a bare key)
        schema_ids = [s for s in _ids(table.get("conformsTo")) if "schema" in s]
        assert schema_ids, "condition table must conformTo a schema entity"
        schema = by_id[schema_ids[0]]
        stype = schema["@type"] if isinstance(schema["@type"], list) else [schema["@type"]]
        assert "csvw:Schema" in stype

        cols = [by_id[cid] for cid in _ids(schema.get("columns"))]
        by_title = {c["titles"]: c for c in cols}
        assert set(by_title) == {"cell_line", "compound", "concentration", "unit", "duration"}
        # every column is typed: datatype + propertyUrl
        for col in by_title.values():
            assert col["datatype"]
            assert col["propertyUrl"]
        # entity-resolving columns carry references to the resolved entities
        assert "#MolecularEntity_chem_1" in str(by_title["compound"].get("valueUrl"))
        assert "#Sample_sample_cult" in str(by_title["cell_line"].get("valueUrl"))

    def test_endpoint_readout_result_is_file(self, tmp_path):
        state = self._state_with_process(
            "EndpointReadout",
            samples="sample_x",
            result="file_raw",
            endpoint="viability",
            detection_instrument="plate reader",
        )
        state.add_entity(_ent("sample_x", "Sample", name="exposed"))
        state.add_entity(
            _ent(
                "file_raw",
                "File",
                name="raw.csv",
                dest_path="assays/a1/dataset/raw_data/raw.csv",
            )
        )
        _, by_id = _build(state, tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        assert proc["additionalType"] == "EndpointReadout"
        assert "assays/a1/dataset/raw_data/raw.csv" in _ids(proc.get("output"))

    def test_data_analysis(self, tmp_path):
        state = self._state_with_process(
            "DataAnalysis",
            object="file_raw",
            result="file_proc",
            software="R",
            data_processing="normalise",
        )
        state.add_entity(_ent("file_raw", "File", name="raw.csv", dest_path="raw.csv"))
        state.add_entity(_ent("file_proc", "File", name="out.csv", dest_path="out.csv"))
        _, by_id = _build(state, tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        assert proc["additionalType"] == "DataAnalysis"
        assert "raw.csv" in _ids(proc.get("input"))
        assert "out.csv" in _ids(proc.get("output"))

    def test_process_attached_to_assay_via_about(self, tmp_path):
        state = self._state_with_process("CellCulture", cell_line="cell_1")
        state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2"))
        _, by_id = _build(state, tmp_path)
        assert "#LabProcess_proc_1" in _ids(by_id["#Assay_assay_1"].get("about"))

    def test_protocol_synthesized_when_absent(self, tmp_path):
        state = self._state_with_process("CellCulture", cell_line="cell_1")
        state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2"))
        _, by_id = _build(state, tmp_path)
        proto_ref = _ids(by_id["#LabProcess_proc_1"].get("executesLabProtocol"))
        assert proto_ref
        proto = by_id[proto_ref[0]]
        assert proto["@type"] == "LabProtocol"


class TestOntologyAnnotations:
    """AOP on the Study, Key Event on the Assay (paper §Methods, via mentions)."""

    def test_study_annotated_with_aop_entity(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="S", aop="aop_37"))
        state.add_entity(_ent("aop_37", "DefinedTerm", name="AOP 37"))
        _, by_id = _build(state, tmp_path)
        assert "#DefinedTerm_aop_37" in _ids(by_id["#Study_study_1"].get("aop"))

    def test_study_aop_inline_iri(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="S", aop="https://aopwiki.org/aops/37"))
        _, by_id = _build(state, tmp_path)
        assert "https://aopwiki.org/aops/37" in _ids(by_id["#Study_study_1"].get("aop"))

    def test_assay_annotated_with_key_event(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A", key_event="ke_55"))
        state.add_entity(_ent("ke_55", "DefinedTerm", name="KE 55"))
        _, by_id = _build(state, tmp_path)
        assert "#DefinedTerm_ke_55" in _ids(by_id["#Assay_assay_1"].get("keyEvent"))


class TestIdentifiersAndConformance:
    def test_local_ids_are_hash_prefixed(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="S"))
        _, by_id = _build(state, tmp_path)
        assert "#Study_study_1" in by_id
        # bare id must not be emitted (type-qualified fragment is used instead)
        assert "study_1" not in by_id

    def test_person_uses_orcid_uri(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("person_1", "Person", name="Jane Doe", orcid="0000-0002-1825-0097"))
        _, by_id = _build(state, tmp_path)
        assert "https://orcid.org/0000-0002-1825-0097" in by_id

    def test_conformsto_gated_on_validation(self, tmp_path):
        state = CrateState()
        # No validation passes recorded → only RO-Crate 1.1 asserted.
        _, by_id = _build(state, tmp_path)
        descriptor = by_id["ro-crate-metadata.json"]
        conforms = _ids(descriptor.get("conformsTo"))
        assert "https://w3id.org/ro/crate/1.1" in conforms
        assert "https://w3id.org/ro/crate/isa/1.0" not in conforms

    def test_conformsto_includes_profiles_when_validated(self, tmp_path):
        state = CrateState()
        state.validation.isa_passed = True
        state.validation.tox_passed = True
        _, by_id = _build(state, tmp_path)
        descriptor = by_id["ro-crate-metadata.json"]
        conforms = _ids(descriptor.get("conformsTo"))
        assert "https://w3id.org/ro/crate/isa-tox/1.0" in conforms
        # ISA layer is declared with the IRI the shapes actually extend
        assert "https://github.com/nfdi4plants/isa-ro-crate-profile" in conforms
        # each declared profile also exists as a Profile contextual entity
        prof = by_id["https://github.com/nfdi4plants/isa-ro-crate-profile"]
        pt = prof["@type"]
        assert "Profile" in (pt if isinstance(pt, list) else [pt])
