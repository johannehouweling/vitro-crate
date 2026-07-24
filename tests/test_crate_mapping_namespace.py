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
        type=entity_type,  # ty: ignore[invalid-argument-type]
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

            # The single Investigation is now folded onto the root ./ (no
            # separate #Investigation_id_01 node), so only the Study and Assay
            # carry the id_01 fragment — each type-qualified and distinct.
            id_entries = [
                e for e in graph
                if "id_01" in e.get("@id", "")
            ]
            assert len(id_entries) == 2, (
                f"Expected 2 distinct @id entries for 'id_01', got {len(id_entries)}"
            )
            assert len({e["@id"] for e in id_entries}) == 2, (
                "Not all @ids are distinct"
            )
            # The Investigation is the root, carrying additionalType Investigation.
            root = next(e for e in graph if e.get("@id") == "./")
            assert root.get("additionalType") == "Investigation"

    def test_colliding_bare_ids_still_emit_unique_ids(self):
        """DEFENSIVE regression guard for the type-qualified @id scheme (Issue #57):
        two CrateState entities that happen to share a bare ``entity_id`` (``cell_01``)
        across different types still emit as @id-UNIQUE nodes (``#Sample_cell_01`` vs
        ``#CellLineSample_cell_01``), satisfying RO-Crate 1.2's "the @graph MUST NOT
        list multiple entities with the same @id" (§Core-Metadata).

        This is NOT a modelling recommendation, and it does not test reference
        *resolution* (the mapper's ``_resolve_many`` fallback is order-based, not
        context-aware). RO-Crate 1.2 in fact discourages the situation this guards: two
        conceptually-different entities SHOULD NOT share an identifier (§Contextual-
        entities), and a single entity that is genuinely both a Sample and a
        CellLineSample should be ONE node with a ``@type`` array — a case this test does
        not cover (see the follow-up design question in #343 discussion).
        """
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