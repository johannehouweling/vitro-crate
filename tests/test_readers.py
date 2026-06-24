"""Tests for input readers."""

from __future__ import annotations

import json

from builder.readers.directory import read_directory
from builder.readers.existing_crate import read_existing_crate
from builder.readers.metadata_files import read_metadata_files
from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _graph(crate_dir):
    with open(crate_dir / "ro-crate-metadata.json") as f:
        g = json.load(f)["@graph"]
    return {e["@id"]: e for e in g}


def _ids(value):
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [v.get("@id") if isinstance(v, dict) else v for v in items]


class TestRoundTrip:
    """build → read_existing_crate → build must be idempotent and structure-preserving."""

    def _state(self):
        state = CrateState()
        state.metadata.accession = "FAB-2026"
        state.add_entity(_ent("inv_1", "Investigation", name="Inv"))
        state.add_entity(_ent("study_1", "Study", name="S", investigation_id="inv_1"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        state.add_entity(_ent("raw", "File", name="raw.csv", dest_path="data/raw.csv"))
        state.add_entity(
            _ent("er", "LabProcess", process_type="EndpointReadout", name="R",
                 assay_id="assay_1", result=["raw"])
        )
        return state

    def test_entity_ids_and_identifiers_are_stable(self, tmp_path):
        build_crate(self._state(), str(tmp_path / "c1"))
        by1 = _graph(tmp_path / "c1")
        restored = read_existing_crate(str(tmp_path / "c1"))
        build_crate(restored, str(tmp_path / "c2"))
        by2 = _graph(tmp_path / "c2")

        # The Study/Assay @ids and identifiers do NOT drift across the round-trip
        # (no #Study_Study_study_1 double-prefixing).
        assert "#Study_study_1" in by2
        assert "#Assay_assay_1" in by2
        assert "#Study_Study_study_1" not in by2
        assert by1["#Study_study_1"]["identifier"] == by2["#Study_study_1"]["identifier"]
        assert by1["#Assay_assay_1"]["identifier"] == by2["#Assay_assay_1"]["identifier"]

    def test_assay_stays_under_study_after_roundtrip(self, tmp_path):
        build_crate(self._state(), str(tmp_path / "c1"))
        restored = read_existing_crate(str(tmp_path / "c1"))
        build_crate(restored, str(tmp_path / "c2"))
        by2 = _graph(tmp_path / "c2")

        # Study → Assay linkage survives (assay not promoted to the root).
        assert "#Assay_assay_1" in _ids(by2["#Study_study_1"].get("hasPart"))
        assert "#Assay_assay_1" not in _ids(by2["./"].get("hasPart"))

    def test_result_file_stays_under_assay_after_roundtrip(self, tmp_path):
        build_crate(self._state(), str(tmp_path / "c1"))
        restored = read_existing_crate(str(tmp_path / "c1"))
        build_crate(restored, str(tmp_path / "c2"))
        by2 = _graph(tmp_path / "c2")

        # The raw file stays attached to its Assay, not orphaned onto the root.
        assert "data/raw.csv" in _ids(by2["#Assay_assay_1"].get("hasPart"))
        assert "data/raw.csv" not in _ids(by2["./"].get("hasPart"))

    def test_single_investigation_not_duplicated_after_roundtrip(self, tmp_path):
        build_crate(self._state(), str(tmp_path / "c1"))
        restored = read_existing_crate(str(tmp_path / "c1"))
        build_crate(restored, str(tmp_path / "c2"))
        by2 = _graph(tmp_path / "c2")
        investigations = [n for n in by2.values() if n.get("additionalType") == "Investigation"]
        assert len(investigations) == 1
        assert investigations[0]["@id"] == "./"


class TestReadDirectory:
    """Tests for read_directory."""

    def test_returns_cratestate_with_session_id(self, tmp_path):
        """read_directory returns a CrateState with session_id."""
        d = tmp_path / "data"
        d.mkdir()
        state = read_directory(str(d))
        assert isinstance(state, CrateState)
        assert state.session_id != ""

    def test_scans_files(self, tmp_path):
        """read_directory scans files in the directory."""
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.csv").write_text("a,b\n1,2\n")
        state = read_directory(str(d))
        assert len(state.scanned_files) == 1
        assert state.scanned_files[0].filename == "test.csv"

    def test_handles_nonexistent_directory(self):
        """read_directory handles non-existent dir without crashing."""
        state = read_directory("/tmp/nonexistent_xyz_reader_test")
        assert isinstance(state, CrateState)
        assert len(state.scanned_files) == 0


class TestReadMetadataFiles:
    """Tests for read_metadata_files (stub)."""

    def test_returns_state_unchanged(self):
        """read_metadata_files returns the state unchanged (stub)."""
        state = CrateState()
        result = read_metadata_files(state)
        assert result is state


class TestReadExistingCrate:
    """Tests for read_existing_crate."""

    def test_reconstructs_entities_from_valid_crate(self, tmp_path):
        """read_existing_crate reconstructs entities from valid ro-crate-metadata.json."""
        from builder.tools.builder import build_crate

        # Build a crate first
        state = CrateState()
        from builder.state import Entity, EntityProvenance

        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"name": "Test Inv"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(inv)
        state.metadata.title = "Test"

        crate_dir = tmp_path / "crate"
        build_crate(state, str(crate_dir))

        # Now read it back
        restored = read_existing_crate(str(crate_dir))
        assert len(restored.list_entities()) >= 1
        assert restored.metadata.title == "Test"

    def test_returns_empty_state_for_nonexistent_directory(self):
        """read_existing_crate returns empty state for non-existent dir."""
        state = read_existing_crate("/tmp/nonexistent_crate_xyz")
        assert isinstance(state, CrateState)
        assert len(state.list_entities()) == 0
