"""Tests for input readers."""

from __future__ import annotations

import json

import pytest

from builder.readers.directory import read_directory
from builder.readers.existing_crate import read_existing_crate
from builder.readers.metadata_files import read_metadata_files
from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate

# TestRoundTrip exports twice per test, and every export runs the uncached,
# owlrl-heavy validator over all three profiles — ~9s per sweep locally at
# REQUIRED alone, and the 2-vCPU CI runner is slower still. Widening the export
# gate to the full tier sweep added ~20% on top, which tipped the two-export
# tests over the CI-wide `--timeout=30`. Same headroom, for the same reason, as
# the other export-heavy modules already take (test_export_smoke,
# test_pipeline_e2e, test_csvw_payload, …). Headroom, not a licence to grow:
# the tests themselves are unchanged.
pytestmark = pytest.mark.timeout(120)


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

    def _state_nested_file(self):
        """A state whose File lives at a NESTED in-crate path (not data/<name>)."""
        state = CrateState()
        state.metadata.accession = "FAB-2026"
        state.add_entity(_ent("inv_1", "Investigation", name="Inv"))
        state.add_entity(_ent("study_1", "Study", name="S", investigation_id="inv_1"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        state.add_entity(
            _ent(
                "raw",
                "File",
                name="raw.prism",
                dest_path="assays/assay_1/dataset/raw_data/raw.prism",
            )
        )
        state.add_entity(
            _ent("er", "LabProcess", process_type="EndpointReadout", name="R",
                 assay_id="assay_1", result=["raw"])
        )
        return state

    def test_nested_file_path_preserved_after_roundtrip(self, tmp_path):
        """A File at a nested in-crate path keeps that path across build→read→build.

        Regression for #180: the reader used to drop the File's location, so a
        rebuild re-placed it at the default ``data/<name>`` — non-idempotent file
        placement. The File's @id (its crate-relative path) must survive verbatim.
        """
        nested = "assays/assay_1/dataset/raw_data/raw.prism"
        build_crate(self._state_nested_file(), str(tmp_path / "c1"))
        by1 = _graph(tmp_path / "c1")
        assert nested in by1  # sanity: the build itself places it there

        restored = read_existing_crate(str(tmp_path / "c1"))
        build_crate(restored, str(tmp_path / "c2"))
        by2 = _graph(tmp_path / "c2")

        # The File's @id (path) is STABLE — no drift to data/raw.prism.
        assert nested in by2
        assert "data/raw.prism" not in by2
        # And it stays nested under its Assay, not orphaned onto the root.
        assert nested in _ids(by2["#Assay_assay_1"].get("hasPart"))
        assert nested not in _ids(by2["./"].get("hasPart"))


class TestReaderFileDestPath:
    """read_existing_crate must feed the builder the File's original path (#180)."""

    def test_reader_sets_dest_path_from_node_id(self, tmp_path):
        """A File node's relative @id becomes the reconstructed File's dest_path.

        ``_file_dest`` (the crate-mapping) places a File from ``dest_path`` first;
        if the reader leaves it unset, a rebuild defaults to ``data/<name>``.
        """
        from builder.state import Entity, EntityProvenance

        state = CrateState()
        state.metadata.title = "Test"
        state.add_entity(
            Entity(
                entity_id="raw",
                type="File",
                fields={"name": "raw.prism", "dest_path": "results/run1/raw.prism"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        build_crate(state, str(tmp_path / "c1"))

        restored = read_existing_crate(str(tmp_path / "c1"))
        files = restored.list_entities("File")
        raw = next(f for f in files if f.fields.get("name") == "raw.prism")
        assert raw.fields.get("dest_path") == "results/run1/raw.prism"

    def test_reader_does_not_set_dest_path_for_fragment_id(self, tmp_path):
        """A path-less File (a ``#``-fragment @id) gets no dest_path — default applies.

        We don't invent a path from a non-relative @id (D5); the builder then
        falls back to its ``data/<name>`` default, the prior behaviour.
        """
        crate_dir = tmp_path / "crate"
        crate_dir.mkdir()
        (crate_dir / "ro-crate-metadata.json").write_text(
            json.dumps(
                {
                    "@context": "https://w3id.org/ro/crate/1.2/context",
                    "@graph": [
                        {
                            "@id": "ro-crate-metadata.json",
                            "@type": "CreativeWork",
                            "conformsTo": {"@id": "https://w3id.org/ro/crate/1.2"},
                            "about": {"@id": "./"},
                        },
                        {"@id": "./", "@type": "Dataset", "name": "X"},
                        {"@id": "#File_floating", "@type": "File", "name": "ghost.bin"},
                    ],
                }
            )
        )
        restored = read_existing_crate(str(crate_dir))
        ghost = next(
            f for f in restored.list_entities("File") if f.fields.get("name") == "ghost.bin"
        )
        assert "dest_path" not in ghost.fields


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
    """`read_metadata_files` is an UNIMPLEMENTED stub — it parses no JSON/YAML yet.

    These tests only pin the current no-op contract; they are NOT metadata-reader
    coverage (no metadata file crosses the boundary). When the reader is implemented,
    replace this with a real test that writes a .json/.yaml metadata file into a
    scanned dir and asserts the entities it extracts (cf. the real-input pattern in
    tests/test_pipeline_real_input.py).
    """

    def test_stub_is_a_noop_and_mutates_nothing(self):
        """The stub returns the SAME state object and adds no entities (no reader runs)."""
        state = CrateState()
        result = read_metadata_files(state)
        assert result is state
        assert result.list_entities() == []  # nothing parsed, nothing added


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

        # Now read it back — reconstruction must be CORRECT, not just non-empty.
        restored = read_existing_crate(str(crate_dir))

        invs = restored.list_entities("Investigation")
        assert len(invs) == 1
        assert invs[0].type == "Investigation"
        # The single Investigation folds onto the RO-Crate root ./, so on read-back its
        # name is the round-tripped crate title, and the title itself round-trips.
        assert invs[0].fields.get("name") == "Test"
        assert restored.metadata.title == "Test"

    def test_returns_empty_state_for_nonexistent_directory(self):
        """read_existing_crate returns empty state for non-existent dir."""
        state = read_existing_crate("/tmp/nonexistent_crate_xyz")
        assert isinstance(state, CrateState)
        assert len(state.list_entities()) == 0
