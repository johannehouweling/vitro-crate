"""Tests for namespace-minted @ids in _crate_mapping — Issue #57.

Two entities of different types with the same entity_id must produce
distinct @id values and distinct idx entries so ro-crate-py does not
silently merge them in the graph.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate



# Every test here exports a crate, and each export now runs the uncached,
# owlrl-heavy validator over all three profiles at the full severity gate (#446)
# — ~10s per export locally, and the 2-vCPU CI runner is ~2-3x slower, which puts
# the whole module against the CI-wide `--timeout=30`. Same headroom, for the
# same reason, that the other export-heavy modules already take
# (test_export_smoke, test_readers, test_path_traversal, test_html_xss).
# Headroom, not a licence to grow: no test in this module is changed.
pytestmark = pytest.mark.timeout(120)

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
        entities). A genuine cell-line sample is ONE ``CellLineSample`` entity, which
        already emits as a single Sample node (``@type: Sample`` + ``additionalType:
        CellLine``); the separate-Sample-plus-CellLineSample input this guards against
        is itself the mis-modeling (design follow-up: #366).
        """
        state = CrateState()
        state.metadata.title = "Reference Test"

        sample = _entity("cell_01", "Sample", name="Sample A")
        # No accession: an accession-backed CellLineSample takes its Cellosaurus
        # IRI as @id and never enters the minted-id scheme this test guards.
        cell_line = _entity("cell_01", "CellLineSample", name="HepG2")
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

class TestSnakeCaseFieldsAreCheckedAgainstTheContext:
    """``_scalar_props`` keeps real vocabulary, renames misspellings, drops the rest.

    The rule started as "any key with an underscore is not a JSON-LD term". That
    is nearly true — but the AOP-Wiki vocabulary in the ISA-Tox context IS
    snake_case, so the syntactic version silently emptied every materialised AOP
    subgraph. Membership in the ``@context`` is the authority, not the key shape.

    Dropping outright was still too blunt. Almost every other context term is
    camelCase, so a snake_case key is usually the SAME property spelled the way
    an agent says it out loud: `measurement_method` is `measurementMethod`.
    Deleting it threw away an answer the crate then reported as missing — one
    session had the agent state what the assay measures, the build delete it,
    and the maturity report ask for it. So a key whose camelCase form is a real
    term is renamed to it, and only a key that is no term under EITHER spelling
    is dropped.
    """

    def test_context_defined_snake_case_terms_survive(self) -> None:
        from builder.tools._crate_mapping import _context_terms, _scalar_props

        # Pinned so this cannot pass vacuously if the context stops defining them.
        assert {"has_molecular_initiating_event", "upstream_event"} <= _context_terms()

        entity = _entity(
            "aop610", "AOP", has_molecular_initiating_event="ke1", short_name="MIE"
        )
        props = _scalar_props(entity)
        assert props.get("short_name") == "MIE"

    def test_a_misspelt_real_term_is_recovered_under_its_real_name(self) -> None:
        """The value is right; only the spelling is wrong. Keep the value."""
        from builder.tools._crate_mapping import _context_terms, _scalar_props

        assert "release_date" not in _context_terms()
        assert "releaseDate" in _context_terms()

        entity = _entity("inv1", "Investigation", release_date="2025-11-10", name="Study")
        props = _scalar_props(entity)
        assert props.get("releaseDate") == "2025-11-10"
        assert "release_date" not in props, "the misspelling must not ship as a key"

    def test_a_key_that_is_no_term_either_way_is_dropped(self) -> None:
        from builder.tools._crate_mapping import _camel_case, _context_terms, _scalar_props

        for invented in ("works_for", "test_type"):
            assert invented not in _context_terms()
            assert _camel_case(invented) not in _context_terms()

        entity = _entity("p1", "Person", works_for="Utrecht", test_type="in vitro", name="Ada")
        props = _scalar_props(entity)
        assert "works_for" not in props and "worksFor" not in props
        assert "test_type" not in props and "testType" not in props
        assert props.get("name") == "Ada", "a real term beside an invented one still ships"

    def test_an_explicit_camel_case_value_wins(self) -> None:
        """That one was written deliberately; the snake variant is the guess."""
        from builder.tools._crate_mapping import _scalar_props

        entity = _entity(
            "inv1",
            "Investigation",
            releaseDate="2025-11-10",
            release_date="1999-01-01",
            name="Study",
        )
        props = _scalar_props(entity)
        assert props.get("releaseDate") == "2025-11-10"

    def test_a_reference_field_is_left_to_its_own_pipeline(self) -> None:
        """`measurementMethod` resolves to a DefinedTerm; a scalar would bypass that."""
        from builder.tools._crate_mapping import _REF_FIELDS, _scalar_props

        assert "measurementMethod" in _REF_FIELDS
        entity = _entity("assay1", "Assay", measurement_method="Gamma counter", name="A")
        props = _scalar_props(entity)
        assert "measurementMethod" not in props
        assert "measurement_method" not in props
