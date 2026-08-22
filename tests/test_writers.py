"""Tests for output writers."""

from __future__ import annotations

import json

from builder.state import CrateState, Entity, EntityProvenance
from builder.writers.rocrate_writer import write_rocrate


class TestWriteRocrate:
    """Tests for write_rocrate."""

    def test_creates_output_directory_and_metadata(self, tmp_path):
        """write_rocrate creates output directory and ro-crate-metadata.json."""
        state = CrateState()
        state.metadata.title = "Test Crate"

        out_dir = tmp_path / "output"
        result = write_rocrate(state, str(out_dir))

        assert result["success"] is True
        assert (out_dir / "ro-crate-metadata.json").exists()

    def test_returns_correct_entity_count(self, tmp_path):
        """write_rocrate returns correct entity_count."""
        state = CrateState()
        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"name": "Test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(inv)

        out_dir = tmp_path / "output"
        result = write_rocrate(state, str(out_dir))
        assert result["entity_count"] == 1

    def test_handles_empty_state(self, tmp_path):
        """write_rocrate handles empty state gracefully."""
        state = CrateState()
        out_dir = tmp_path / "output"
        result = write_rocrate(state, str(out_dir))
        assert result["success"] is True
        assert result["entity_count"] == 0

    def test_writes_valid_json(self, tmp_path):
        """write_rocrate writes valid JSON with @context and @graph."""
        state = CrateState()
        out_dir = tmp_path / "output"
        write_rocrate(state, str(out_dir))

        with open(out_dir / "ro-crate-metadata.json") as f:
            data = json.load(f)
        assert "@context" in data
        assert "@graph" in data
