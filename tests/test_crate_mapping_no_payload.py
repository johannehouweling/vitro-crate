"""Tests for the in-memory (no-payload) assembly path in _crate_mapping (Issue #87).

``populate_crate`` materialises exactly one payload artefact on disk: the CSVW
condition table for an ``Exposure`` LabProcess (``_synth_condition_table``). The
in-memory ``build_and_validate`` path must assemble the *graph* without writing
that file, so the validator can run on the metadata dict with zero disk writes.
"""

from __future__ import annotations

from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import populate_crate
from profiles.context import ISA_TOX_CONTEXT


def _exposure_state() -> CrateState:
    """A state with a single Exposure LabProcess (triggers condition-table synth)."""
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


def _assemble(state, output_dir, *, materialize_payload):
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, output_dir, materialize_payload=materialize_payload)
    return crate


def _condition_table_nodes(crate: ROCrate) -> list[dict]:
    graph = crate.metadata.generate()["@graph"]
    return [e for e in graph if str(e.get("@id", "")).endswith("condition_table.csv")]


def test_no_payload_writes_nothing(tmp_path):
    """With materialize_payload=False the Exposure condition table is NOT written."""
    crate = _assemble(_exposure_state(), tmp_path, materialize_payload=False)
    # No CSV (or any file) materialised on disk.
    assert list(tmp_path.iterdir()) == []
    # But the condition-table File node still exists in the graph so the
    # metadata document is structurally complete for validation.
    assert _condition_table_nodes(crate), "condition table node must be in the graph"


def test_no_payload_with_none_output_dir(tmp_path, monkeypatch):
    """materialize_payload=False works even when output_dir is None (pure memory)."""
    monkeypatch.chdir(tmp_path)
    crate = _assemble(_exposure_state(), None, materialize_payload=False)
    assert list(tmp_path.iterdir()) == []
    assert _condition_table_nodes(crate)


def test_default_materializes_payload(tmp_path):
    """Default (materialize_payload=True) still writes the condition-table CSV."""
    _assemble(_exposure_state(), tmp_path, materialize_payload=True)
    csvs = list(tmp_path.rglob("*condition_table.csv"))
    assert csvs, "condition table CSV must be written to disk by default"
