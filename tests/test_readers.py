"""Tests for input readers."""

from __future__ import annotations

from builder.readers.directory import read_directory
from builder.readers.existing_crate import read_existing_crate
from builder.readers.metadata_files import read_metadata_files
from builder.state import CrateState


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
