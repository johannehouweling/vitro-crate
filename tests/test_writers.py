"""Tests for output writers."""

from __future__ import annotations

import json
from pathlib import Path

from builder.writers.rocrate_writer import write_rocrate
from builder.writers.arc_writer import write_arc
from builder.state import CrateState, Entity, EntityProvenance


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
            entity_id="inv_001", type="Investigation",
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


class TestWriteArc:
    """Tests for write_arc."""

    def test_creates_basic_arc_structure(self, tmp_path):
        """write_arc creates basic ARC directory with ro-crate-metadata.json."""
        state = CrateState()
        state.metadata.title = "Test ARC"

        out_dir = tmp_path / "arc_output"
        result = write_arc(state, str(out_dir))

        assert result["success"] is True
        assert (out_dir / "ro-crate-metadata.json").exists()

    def test_creates_study_directories(self, tmp_path):
        """write_arc creates study directories for Study entities."""
        state = CrateState()
        study = Entity(
            entity_id="study_001", type="Study",
            fields={"name": "My Study"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(study)

        out_dir = tmp_path / "arc_output"
        write_arc(state, str(out_dir))

        assert (out_dir / "studies" / "study_001").is_dir()

    def test_creates_assay_directories(self, tmp_path):
        """write_arc creates assay directories with dataset subdirs for Assay entities."""
        state = CrateState()
        assay = Entity(
            entity_id="assay_001", type="Assay",
            fields={"name": "My Assay"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(assay)

        out_dir = tmp_path / "arc_output"
        write_arc(state, str(out_dir))

        assert (out_dir / "assays" / "assay_001").is_dir()
        assert (out_dir / "assays" / "assay_001" / "dataset" / "raw_data").is_dir()
        assert (out_dir / "assays" / "assay_001" / "dataset" / "processed_data").is_dir()
