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
    ("exposure_duration", "string", "http://purl.obolibrary.org/obo/NCIT_C83280", None),
    ("experiment", "string", "http://www.ebi.ac.uk/efo/EFO_0002091", None),
    ("technical_replicate", "string", "http://www.ebi.ac.uk/efo/EFO_0002090", None),
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


def test_a_declared_exposure_result_does_not_displace_the_condition_table():
    """A drafter-supplied result is added to, never swapped for, the table (#531).

    The condition table is not decoration: ISA forbids a MolecularEntity as a
    LabProcess object, so a compound reaches the experiment only *through* the
    table (table --about--> compound). Substituting a declared result for it
    severs that route silently — the crate keeps its compounds and loses every
    link to them.

    The EndpointReadout branch already appends rather than substitutes and says
    so in its own comment; this pins the Exposure branch to the same contract.
    """
    state = _exposure_state()
    exposure = next(e for e in state.list_entities("LabProcess") if e.entity_id == "proc_exp")
    exposure.fields["result"] = "file_declared"
    state.add_entity(
        _ent("file_declared", "File", name="declared.csv", path="data/declared.csv")
    )

    graph = _assemble(state)
    by_id = _by_id(graph)
    process = next(
        e for e in graph if str(e.get("@id", "")).endswith("LabProcess_proc_exp")
    )
    results = _ids(process.get("output"))

    assert any(r.endswith("condition_table.csv") for r in results), (
        f"the condition table was displaced by the declared result: {results}"
    )
    assert any("declared" in r for r in results), (
        f"the declared result was dropped: {results}"
    )
    # …and the table still routes the compound to the process.
    table = by_id[next(r for r in results if r.endswith("condition_table.csv"))]
    schema = by_id[[s for s in _ids(table.get("conformsTo")) if "schema" in str(s)][0]]
    cols = {by_id[cid]["titles"]: by_id[cid] for cid in _ids(schema.get("columns"))}
    assert "#MolecularEntity_chem_1" in str(cols["compound"].get("valueUrl"))


def test_an_exposure_without_a_declared_result_still_gets_the_table():
    # The pre-existing path, pinned so the fix cannot regress it.
    graph = _assemble(_exposure_state())
    process = next(
        e for e in graph if str(e.get("@id", "")).endswith("LabProcess_proc_exp")
    )
    assert [r for r in _ids(process.get("output")) if r.endswith("condition_table.csv")]


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


class TestEmptyConditionTableSaysSo:
    """A zero-row condition table declares its emptiness in metadata (#473).

    The header-only table ships the FULL ten-column CSVW schema — datatype and
    propertyUrl on every column, valueUrl on cell_line/compound. Over zero rows
    every one of those claims is *vacuously* true, so a crate-only "is it
    CSVW-typed" check passed tautologically on a deposit whose population had
    failed. The only tell was the AUTOGENERATED name prefix, which is prose.
    """

    @staticmethod
    def _table(graph: list[dict]) -> dict:
        table = next(
            (
                e
                for e in graph
                if str(e.get("@id", "")).endswith("condition_table.csv")
            ),
            None,
        )
        assert table is not None, "no condition table in the graph"
        return table

    def _export(self, tmp_path, rows: list[str] | None):
        """Assemble with payload materialised, optionally pre-populating rows."""
        from builder.tools._crate_mapping import _condition_table_rel, populate_crate

        rel = _condition_table_rel("#LabProcess_proc_exp")
        if rows is not None:
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            from builder.tools._crate_mapping import _CONDITION_TABLE_HEADER

            dest.write_text(_CONDITION_TABLE_HEADER + "".join(rows), encoding="utf-8")

        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(_exposure_state(), crate, tmp_path, materialize_payload=True)
        return crate.metadata.generate()["@graph"]

    def test_zero_row_table_carries_the_note(self, tmp_path):
        table = self._table(self._export(tmp_path, rows=None))
        assert "NO rows" in str(table.get("description", "")), table.get("description")

    def test_a_populated_table_does_not(self, tmp_path):
        """The note is not stamped once real conditions exist.

        Guards the direction that actually matters: a stale emptiness claim on a
        populated table would be worse than the original bug, because it is a
        false statement rather than a missing one.
        """
        row = "W1,assay,HepG2,T4,10,uM,24h,exp1,1,no\n"
        table = self._table(self._export(tmp_path, rows=[row]))
        assert "NO rows" not in str(table.get("description", "") or "")

    def test_blank_lines_are_not_conditions(self, tmp_path):
        """A trailing newline is not a captured condition.

        A spreadsheet export routinely leaves one; counting it would silently
        suppress the note on a table that is in fact empty.
        """
        table = self._table(self._export(tmp_path, rows=["\n", ",,,,,,,,,\n"]))
        assert "NO rows" in str(table.get("description", "")), table.get("description")

    def test_the_in_memory_path_makes_no_claim(self):
        """No CSV to consult means no assertion either way.

        "We did not look" is a different claim from "there are no rows", and the
        crate must not assert emptiness it never verified (D5).
        """
        table = self._table(_assemble(_exposure_state()))
        assert "NO rows" not in str(table.get("description", "") or "")


def test_property_url_stays_an_id_reference_not_a_bare_string():
    """propertyUrl must be ``{"@id": …}``, not the bare URI string.

    A bare string reads as the faithful CSVW form — propertyUrl IS typed as a
    URI — and the terms look like vocabulary the crate merely cites. But some of
    them the crate DOES describe: a CellLineSample materialises NCIT_C16403 as a
    `cell line` DefinedTerm, and the base profile then reports "references
    NCIT_C16403 as a string" and fails the whole pass.

    The sibling assertion in ``test_condition_table_emits_ten_typed_csvw_columns``
    goes through ``_iri()``, which unwraps either form and so cannot see the
    difference. This one looks at the raw value, because the difference is the
    entire point.
    """
    by_id = _by_id(_assemble(_exposure_state()))
    table = next(
        e for e in by_id.values()
        if str(e.get("@id", "")).endswith("condition_table.csv")
    )
    schema_ids = [s for s in _ids(table.get("conformsTo")) if "schema" in str(s)]
    schema = by_id[schema_ids[0]]
    cols = [by_id[cid] for cid in _ids(schema.get("columns"))]
    assert cols

    cell_line = next(c for c in cols if c["titles"] == "cell_line")
    assert cell_line["propertyUrl"] == {
        "@id": "http://purl.obolibrary.org/obo/NCIT_C16403"
    }
    # Not just the one term that happens to be described today.
    for col in cols:
        if col.get("propertyUrl") is not None:
            assert isinstance(col["propertyUrl"], dict), (
                f"{col['titles']}: propertyUrl must be an @id reference, "
                f"got {col['propertyUrl']!r}"
            )


def test_every_csvw_column_carries_a_name():
    """csvw:Column nodes are Contextual Entities, which RO-Crate requires be named.

    Carrying only ``titles`` earns a finding per column. The column title IS the
    human-readable name, so this states nothing new — it states it under the term
    the base profile reads.
    """
    by_id = _by_id(_assemble(_exposure_state()))
    table = next(
        e for e in by_id.values()
        if str(e.get("@id", "")).endswith("condition_table.csv")
    )
    schema_ids = [s for s in _ids(table.get("conformsTo")) if "schema" in str(s)]
    schema = by_id[schema_ids[0]]
    cols = [by_id[cid] for cid in _ids(schema.get("columns"))]
    assert cols
    for col in cols:
        assert col.get("name") == col["titles"]
