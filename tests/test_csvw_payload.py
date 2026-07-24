"""Tests for the CSVW payload lane (Issue #180, Lane D).

Two deterministic build-path enhancements (no new LLM tools):

(a) The Exposure condition table grows from the original 5 columns to the gold
    crate's full 10-column typed CSVW schema (``well_id``, ``assay``,
    ``cell_line``, ``compound``, ``concentration_value``, ``concentration_unit``,
    ``exposure_duration``, ``experiment``, ``technical_replicate``, ``control``),
    each carrying ``datatype`` + ``propertyUrl`` (and ``valueUrl`` for the
    cell-line / compound columns).

(b) An EndpointReadout that already emits result file(s) additionally emits a
    typed ``raw_measurements.csv`` ``csvw:Table`` (3 columns: ``well_id``,
    ``measured_value``, ``measured_unit``) — typed the same way the condition
    table is. No measurement rows are fabricated (D5): the CSV is a header-only
    placeholder carrying its machine-readable schema.

Graph-only assertions (no ``build_and_validate``) so the suite stays fast.
"""

from __future__ import annotations

import pytest
from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools._crate_mapping import populate_crate
from builder.tools.validation import build_and_validate
from profiles.context import ISA_TOX_CONTEXT

# Only the build-and-validate test below is heavy; mark the module so it cannot
# hang CI.
pytestmark = pytest.mark.timeout(120)


def _ent(entity_id: str, type_: EntityType, **fields: object) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _assemble(state: CrateState) -> list[dict]:
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, None, materialize_payload=False)
    return crate.metadata.generate()["@graph"]


def _by_id(graph: list[dict]) -> dict[str, dict]:
    return {str(e.get("@id", "")): e for e in graph}


def _ids(value: object) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v.get("@id")) if isinstance(v, dict) else str(v) for v in items]


def _iri(value: object) -> object:
    """Unwrap a possibly-{@id} value to its IRI string."""
    if isinstance(value, dict):
        return value.get("@id")
    return value


# --- Expected gold-crate column contracts -----------------------------------

_EXPECTED_CONDITION_COLUMNS = [
    ("well_id", "string", "http://purl.org/dc/terms/identifier", None),
    ("assay", "string", "http://purl.obolibrary.org/obo/NCIT_C60819", None),
    ("cell_line", "string", "http://purl.obolibrary.org/obo/NCIT_C16403", "cell_line"),
    ("compound", "string", "http://purl.obolibrary.org/obo/CHEBI_23367", "compound"),
    ("concentration_value", "double", "http://purl.obolibrary.org/obo/PATO_0000033", None),
    ("concentration_unit", "string", "http://purl.obolibrary.org/obo/IAO_0000039", None),
    ("exposure_duration", "string", "https://bioregistry.io/NCIT:C83280", None),
    ("experiment", "string", "https://bioregistry.io/EFO:0002091", None),
    ("technical_replicate", "string", "https://bioregistry.io/EFO:0002090", None),
    ("control", "string", "http://purl.obolibrary.org/obo/NCIT_C28143", None),
]

_EXPECTED_RAW_COLUMNS = [
    ("well_id", "string", "http://purl.org/dc/terms/identifier"),
    ("measured_value", "double", "http://purl.obolibrary.org/obo/IAO_0000109"),
    ("measured_unit", "string", "http://purl.obolibrary.org/obo/IAO_0000039"),
]


# --- (a) Condition-table 10-column schema -----------------------------------


def _exposure_state() -> CrateState:
    state = CrateState()
    state.metadata.title = "Exposure crate"
    state.add_entity(_ent("assay_1", "Assay", name="A"))
    state.add_entity(
        _ent(
            "proc_exp",
            "LabProcess",
            name="Exposure step",
            process_type="Exposure",
            assay_id="assay_1",
            samples="sample_cult",
            chemicals="chem_1",
        )
    )
    state.add_entity(_ent("sample_cult", "Sample", name="cultured"))
    state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin A"))
    return state


def test_condition_table_emits_ten_typed_csvw_columns():
    by_id = _by_id(_assemble(_exposure_state()))
    table = next(
        e for e in by_id.values()
        if str(e.get("@id", "")).endswith("condition_table.csv")
    )
    schema_ids = [s for s in _ids(table.get("conformsTo")) if "schema" in str(s)]
    assert schema_ids
    schema = by_id[schema_ids[0]]
    cols = [by_id[cid] for cid in _ids(schema.get("columns"))]
    by_title = {c["titles"]: c for c in cols}

    assert [c["titles"] for c in cols] == [
        t for (t, _d, _p, _v) in _EXPECTED_CONDITION_COLUMNS
    ]
    for title, datatype, prop, _vt in _EXPECTED_CONDITION_COLUMNS:
        col = by_title[title]
        assert col["@type"] == "csvw:Column"
        assert col["datatype"] == datatype
        # propertyUrl is emitted as an {@id} reference (RO-Crate 1.2).
        assert _iri(col["propertyUrl"]) == prop


def test_condition_table_value_urls_resolve_to_entities():
    """cell_line / compound columns still resolve their valueUrl to entity ids."""
    by_id = _by_id(_assemble(_exposure_state()))
    table = next(
        e for e in by_id.values()
        if str(e.get("@id", "")).endswith("condition_table.csv")
    )
    schema = by_id[[s for s in _ids(table.get("conformsTo")) if "schema" in str(s)][0]]
    cols = {by_id[cid]["titles"]: by_id[cid] for cid in _ids(schema.get("columns"))}
    assert "#MolecularEntity_chem_1" in str(cols["compound"].get("valueUrl"))
    assert "#Sample_sample_cult" in str(cols["cell_line"].get("valueUrl"))


# --- (b) raw_measurements typed csvw:Table ----------------------------------


def _endpoint_readout_state() -> CrateState:
    state = CrateState()
    state.add_entity(_ent("assay_1", "Assay", name="A"))
    state.add_entity(
        _ent(
            "proc_er",
            "LabProcess",
            name="Endpoint Readout",
            process_type="EndpointReadout",
            assay_id="assay_1",
            samples="sample_x",
            result="file_raw",
            endpoint="viability",
        )
    )
    state.add_entity(_ent("sample_x", "Sample", name="exposed"))
    state.add_entity(
        _ent("file_raw", "File", name="raw.csv", dest_path="data/raw.csv")
    )
    return state


def test_endpoint_readout_emits_typed_raw_measurements_table():
    by_id = _by_id(_assemble(_endpoint_readout_state()))
    proc = by_id["#LabProcess_proc_er"]
    out_ids = _ids(proc.get("output"))
    # Explicit result file is preserved …
    assert "data/raw.csv" in out_ids
    # … and a typed raw_measurements table is emitted alongside it.
    rm_ids = [i for i in out_ids if str(i).endswith("raw_measurements.csv")]
    assert rm_ids, f"expected a raw_measurements.csv output, got {out_ids}"
    rm = by_id[rm_ids[0]]
    tt = rm["@type"] if isinstance(rm["@type"], list) else [rm["@type"]]
    assert "File" in tt and "csvw:Table" in tt


def test_raw_measurements_schema_has_three_typed_columns():
    by_id = _by_id(_assemble(_endpoint_readout_state()))
    proc = by_id["#LabProcess_proc_er"]
    rm_id = next(
        i for i in _ids(proc.get("output")) if str(i).endswith("raw_measurements.csv")
    )
    rm = by_id[rm_id]
    schema_ids = [s for s in _ids(rm.get("conformsTo")) if "schema" in str(s)]
    assert schema_ids, "raw_measurements must conformTo a schema entity"
    schema = by_id[schema_ids[0]]
    stype = schema["@type"] if isinstance(schema["@type"], list) else [schema["@type"]]
    assert "csvw:Schema" in stype
    cols = [by_id[cid] for cid in _ids(schema.get("columns"))]
    got = [(c["titles"], c["datatype"], _iri(c["propertyUrl"])) for c in cols]
    assert got == _EXPECTED_RAW_COLUMNS
    for c in cols:
        assert c["@type"] == "csvw:Column"


def test_endpoint_readout_with_raw_measurements_validates_clean():
    """The synthesized raw_measurements csvw:Table passes full ISA-Tox SHACL."""
    state = CrateState()
    state.metadata.title = "Readout crate"
    state.add_entity(_ent("inv_1", "Investigation", name="Inv"))
    state.add_entity(_ent("study_1", "Study", name="Study", investigation_id="inv_1"))
    state.add_entity(_ent("assay_1", "Assay", name="Assay", study_id="study_1"))
    state.add_entity(
        _ent(
            "proc_er",
            "LabProcess",
            name="Endpoint Readout",
            process_type="EndpointReadout",
            assay_id="assay_1",
            samples="sample_x",
            result="file_raw",
            endpoint="viability",
            detection_instrument="plate reader",
        )
    )
    state.add_entity(_ent("sample_x", "Sample", name="exposed"))
    state.add_entity(_ent("file_raw", "File", name="raw.csv", dest_path="data/raw.csv"))

    result = build_and_validate(state, severity="required")
    assert result["ok"] is True, result["issues"]
    assert result["conformance"] == {"base": True, "isa": True, "tox": True}


def test_endpoint_readout_without_result_emits_no_raw_measurements():
    """No explicit result => no synthesized table (D5; preserves repair contract)."""
    state = CrateState()
    state.add_entity(_ent("assay_1", "Assay", name="A"))
    state.add_entity(
        _ent(
            "proc_er",
            "LabProcess",
            name="Endpoint Readout",
            process_type="EndpointReadout",
            assay_id="assay_1",
            samples="sample_x",
            endpoint="viability",
        )
    )
    state.add_entity(_ent("sample_x", "Sample", name="exposed"))
    by_id = _by_id(_assemble(state))
    proc = by_id["#LabProcess_proc_er"]
    out_ids = _ids(proc.get("output"))
    assert not any(str(i).endswith("raw_measurements.csv") for i in out_ids), out_ids
