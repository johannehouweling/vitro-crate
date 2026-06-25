"""Tests for Exposure condition-table population + Frictionless bridge (Issue #144).

``populate_condition_table`` writes per-well rows into the Exposure condition
table CSV (replacing the header-only placeholder from #94). ``csvw_to_frictionless``
converts the CSVW column descriptors into the Frictionless ``{fields: [...]}``
shape so ``validate_table`` needs no hand-authored schema.
"""

from __future__ import annotations

import csv

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import _CONDITION_TABLE_COLUMNS
from builder.tools.data_content import (
    csvw_to_frictionless,
    populate_condition_table,
    validate_table,
)


def _exposure_state() -> CrateState:
    state = CrateState()
    state.metadata.title = "Exposure crate"
    state.add_entity(
        Entity(
            entity_id="proc_exp",
            type="LabProcess",
            fields={"process_type": "Exposure", "name": "Exposure step"},
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


def test_csvw_to_frictionless_maps_columns():
    schema = csvw_to_frictionless(_CONDITION_TABLE_COLUMNS)
    assert "fields" in schema
    names = [f["name"] for f in schema["fields"]]
    # The full 10-column condition-table schema (Issue #180, Lane D).
    assert names == [
        "well_id",
        "assay",
        "cell_line",
        "compound",
        "concentration_value",
        "concentration_unit",
        "exposure_duration",
        "experiment",
        "technical_replicate",
        "control",
    ]
    by_name = {f["name"]: f for f in schema["fields"]}
    # double -> number, string -> string (Frictionless types)
    assert by_name["concentration_value"]["type"] == "number"
    assert by_name["cell_line"]["type"] == "string"


def test_populate_condition_table_writes_rows(tmp_path):
    state = _exposure_state()
    # Population fills whatever columns the data provides; the schema describes
    # all 10, missing columns are written empty (extrasaction="ignore").
    rows = [
        {"well_id": "A1", "cell_line": "HepG2", "compound": "Aspirin",
         "concentration_value": "10", "concentration_unit": "uM",
         "exposure_duration": "24h"},
        {"well_id": "A2", "cell_line": "HepG2", "compound": "Aspirin",
         "concentration_value": "100", "concentration_unit": "uM",
         "exposure_duration": "24h"},
    ]
    result = populate_condition_table(state, "proc_exp", rows, output_dir=str(tmp_path))
    assert result["ok"] is True
    csv_path = result["path"]
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = list(csv.DictReader(fh))
    assert len(reader) == 2
    assert reader[0]["concentration_value"] == "10"
    assert reader[1]["concentration_value"] == "100"
    assert reader[0]["well_id"] == "A1"


def test_populated_table_validates_with_inferred_schema(tmp_path):
    state = _exposure_state()
    rows = [
        {"well_id": "A1", "cell_line": "HepG2", "compound": "Aspirin",
         "concentration_value": "10", "concentration_unit": "uM",
         "exposure_duration": "24h"},
    ]
    result = populate_condition_table(state, "proc_exp", rows, output_dir=str(tmp_path))
    schema = csvw_to_frictionless(_CONDITION_TABLE_COLUMNS)
    report = validate_table(result["path"], schema)
    assert report["ok"] is True, report
