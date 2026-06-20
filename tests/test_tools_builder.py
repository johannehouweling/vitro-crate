"""Tests for builder/tools/builder.py — build_crate tool."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from builder.state import CrateState
from builder.tools.builder import build_crate


class TestBuildCrate:
    """Tests for build_crate — assembles a crate directory from CrateState."""

    def test_creates_output_directory(self):
        """build_crate creates the output directory if it doesn't exist."""
        state = CrateState()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "my_crate")
            result = build_crate(state, output_path)

            assert result["success"] is True
            assert result["crate_path"] == output_path
            assert result["error"] is None
            assert Path(output_path).is_dir()

    def test_writes_minimal_ro_crate_metadata_json(self):
        """build_crate writes a ro-crate-metadata.json with basic structure."""
        state = CrateState()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "my_crate")
            result = build_crate(state, output_path)

            assert result["success"] is True
            metadata_path = Path(output_path) / "ro-crate-metadata.json"
            assert metadata_path.exists()

            with open(metadata_path) as f:
                metadata = json.load(f)

            # Must have @context and @graph
            assert "@context" in metadata
            assert "@graph" in metadata
            # Should have a root dataset
            roots = [e for e in metadata["@graph"] if e.get("@type") == "Dataset"]
            assert len(roots) >= 1

    def test_includes_state_metadata_in_crate(self):
        """build_crate includes session metadata from CrateState."""
        state = CrateState()
        state.metadata.title = "My Test Crate"
        state.metadata.description = "A description"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "crate")
            result = build_crate(state, output_path)

            assert result["success"] is True
            metadata_path = Path(output_path) / "ro-crate-metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            # The root dataset should have the name and description
            root = [e for e in metadata["@graph"] if e.get("@type") == "Dataset"][0]
            assert root.get("name") == "My Test Crate"
            assert root.get("description") == "A description"

    def test_returns_error_on_invalid_path(self):
        """build_crate returns error dict on failure."""
        state = CrateState()
        # An empty string path is invalid
        result = build_crate(state, "")

        assert result["success"] is False
        assert result["error"] is not None
        assert result["crate_path"] == ""