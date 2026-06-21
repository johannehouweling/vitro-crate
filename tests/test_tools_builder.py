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
        """build_crate returns error dict on failure (e.g. non-writable path)."""
        state = CrateState()
        state.session_id = "test_invalid_path"
        # An empty string now uses the default, so use a path that will fail
        # (root-owned dir without write permission)
        result = build_crate(state, "/proc/0/crate")

        assert result["success"] is False
        assert result["error"] is not None

    def test_uses_rocrate_py_metadata_descriptor(self):
        """build_crate assembles via ro-crate-py, not a hand-rolled dict.

        ro-crate-py always emits a self-describing RO-Crate Metadata Descriptor
        entity (@id ro-crate-metadata.json, @type CreativeWork) whose conformsTo
        names the RO-Crate 1.1 spec and whose `about` points at the root. A
        hand-rolled graph does not produce this, so its presence proves the
        library is doing the assembly.
        """
        state = CrateState()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "crate")
            result = build_crate(state, output_path)

            assert result["success"] is True
            with open(Path(output_path) / "ro-crate-metadata.json") as f:
                metadata = json.load(f)

            descriptor = next(
                e for e in metadata["@graph"]
                if e.get("@id") == "ro-crate-metadata.json"
            )
            assert descriptor.get("@type") == "CreativeWork"
            assert descriptor.get("about") == {"@id": "./"}
            conforms = descriptor.get("conformsTo")
            conforms_ids = (
                [conforms.get("@id")] if isinstance(conforms, dict)
                else [c.get("@id") for c in conforms or []]
            )
            # ro-crate-py stamps the descriptor with the RO-Crate spec it conforms
            # to; the exact minor version is pinned explicitly in Step 2.
            assert any("w3id.org/ro/crate" in cid for cid in conforms_ids)

    def test_build_crate_returns_crate_path_for_validate(self, monkeypatch):
        """build_crate returns a crate_path that can be passed back to validate.

        build_crate should document this. When output_path is given, the
        returned crate_path matches it exactly.
        """
        state = CrateState()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "my_crate")
            result = build_crate(state, output_path)

            assert result["success"] is True
            # The returned crate_path must equal the output_path we passed,
            # so it can be fed straight into validate().
            assert result["crate_path"] == output_path

    def test_build_crate_generates_default_crate_path_to_sessions(self, monkeypatch):
        """build_crate uses a session-derived default when output_path is not given.

        The default should be: sessions/<session_id>/working_crate/
        """
        state = CrateState()
        state.session_id = "test_default_path_001"

        import builder.tools.builder as _builder_mod

        # Call without output_path — the function should supply a default
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.chdir(tmpdir)
            result = build_crate(state)

            assert result["success"] is True
            expected = f"sessions/{state.session_id}/working_crate"
            assert result["crate_path"] == expected
            assert Path(expected).is_dir()