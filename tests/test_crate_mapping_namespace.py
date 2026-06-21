"""Tests for namespace-minted @ids in _crate_mapping — Issue #57.

Two entities of different types with the same entity_id must produce
distinct @id values and distinct idx entries so ro-crate-py does not
silently merge them in the graph.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate


def _entity(entity_id: str, entity_type: str, **fields) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=entity_type,  # type: ignore[arg-type]
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


class TestMintedIdNamespace:
    """@id collisions between different entity types must be prevented."""

    def test_sample_and_cell_line_sample_same_id_no_merge(self):
        """A Sample and CellLineSample sharing entity_id produce distinct @ids."""
        state = CrateState()
        state.metadata.title = "Namespace Test"
        state.metadata.description = "Testing distinct @ids"

        sample = _entity("my_cell", "Sample", name="Plain Sample")
        cell_line = _entity("my_cell", "CellLineSample", name="HepG2")

        state.add_entity(sample)
        state.add_entity(cell_line)

        # Both must be retrievable independently by type
        all_samples = state.list_entities("Sample")
        all_cell_lines = state.list_entities("CellLineSample")
        assert len(all_samples) == 1
        assert all_samples[0].type == "Sample"
        assert len(all_cell_lines) == 1
        assert all_cell_lines[0].type == "CellLineSample"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "crate")
            result = build_crate(state, output_path)
            assert result["success"] is True

            metadata_path = Path(output_path) / "ro-crate-metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            graph: list[dict] = metadata["@graph"]
            ids = [
                e["@id"]
                for e in graph
                if "my_cell" in e.get("@id", "")
            ]
            assert len(ids) == 2, (
                f"Expected 2 distinct @id entries for 'my_cell', got {len(ids)}: {ids}"
            )
            assert len(set(ids)) == 2, (
                f"@ids are not distinct: {ids}"
            )

    def test_different_types_distinct_minted_ids(self):
        """Entities of different types with the same bare ID get type-qualified @ids."""
        state = CrateState()
        state.metadata.title = "ID Collision Test"

        inv = _entity("id_01", "Investigation", name="My Investigation")
        study = _entity("id_01", "Study", name="My Study")
        assay = _entity("id_01", "Assay", name="My Assay")

        state.add_entity(inv)
        state.add_entity(study)
        state.add_entity(assay)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "crate")
            result = build_crate(state, output_path)
            assert result["success"] is True

            metadata_path = Path(output_path) / "ro-crate-metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            graph: list[dict] = metadata["@graph"]

            id_entries = [
                e for e in graph
                if "id_01" in e.get("@id", "")
            ]
            assert len(id_entries) == 3, (
                f"Expected 3 distinct @id entries for 'id_01', got {len(id_entries)}"
            )
            assert len({e["@id"] for e in id_entries}) == 3, (
                "Not all @ids are distinct"
            )

    def test_reference_resolution_across_collision(self):
        """References to colliding entities resolve to the correct type-qualified node."""
        state = CrateState()
        state.metadata.title = "Reference Test"

        sample = _entity("cell_01", "Sample", name="Sample A")
        cell_line = _entity("cell_01", "CellLineSample", name="HepG2", accession="CVCL_0027")
        person = _entity("p_001", "Person", name="Researcher")

        state.add_entity(sample)
        state.add_entity(cell_line)
        state.add_entity(person)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "crate")
            result = build_crate(state, output_path)
            assert result["success"] is True

            metadata_path = Path(output_path) / "ro-crate-metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            graph: list[dict] = metadata["@graph"]

            ids_with_cell = [e["@id"] for e in graph if "cell_01" in e.get("@id", "")]
            assert len(ids_with_cell) == 2, (
                f"Expected 2 entries with 'cell_01', got {len(ids_with_cell)}: {ids_with_cell}"
            )
            assert len(set(ids_with_cell)) == 2, (
                f"@ids are not distinct: {ids_with_cell}"
            )