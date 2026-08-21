"""Tests for builder/tools/management.py."""

from __future__ import annotations

import logging

import json

import pytest

from builder.state import CrateState, Entity, EntityProvenance, FileClassification
from builder.tools.builder import build_crate
from builder.tools.management import (
    bulk_set_fields,
    find_referrers,
    list_entities,
    list_scanned_files,
    remove_entity,
    set_entity_field,
    update_entity,
)



# Every test here exports a crate, and each export now runs the uncached,
# owlrl-heavy validator over all three profiles at the full severity gate (#446)
# — ~10s per export locally, and the 2-vCPU CI runner is ~2-3x slower, which puts
# the whole module against the CI-wide `--timeout=30`. Same headroom, for the
# same reason, that the other export-heavy modules already take
# (test_export_smoke, test_readers, test_path_traversal, test_html_xss).
# Headroom, not a licence to grow: no test in this module is changed.
pytestmark = pytest.mark.timeout(120)

def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _fc(path, filename, size, mime):
    return FileClassification(
        path=path, filename=filename, size=size, mime_type=mime
    )


class TestListScannedFiles:
    """list_scanned_files retrieves the full inventory the scan tool/pruning hide."""

    def _state(self, n=5):
        state = CrateState()
        state.scanned_files = [
            _fc(f"/data/raw/file{i}.csv", f"file{i}.csv", 100 + i, "text/csv")
            for i in range(n)
        ]
        return state

    def test_returns_full_inventory_compact(self):
        out = list_scanned_files(self._state(5))
        assert out["total_scanned"] == 5
        assert out["matched"] == 5
        assert out["returned"] == 5
        assert len(out["files"]) == 5
        # Compact records only — never the heavy first_rows preview.
        assert set(out["files"][0]) == {"path", "filename", "size", "mime_type"}

    def test_empty_inventory(self):
        out = list_scanned_files(CrateState())
        assert out["total_scanned"] == 0
        assert out["files"] == []

    def test_filter_by_name_contains(self):
        state = CrateState()
        state.scanned_files = [
            _fc("/d/a.csv", "a.csv", 1, "text/csv"),
            _fc("/d/raw_meas.mzML", "raw_meas.mzML", 2, "application/x-mzml"),
        ]
        out = list_scanned_files(state, name_contains="raw")
        assert out["matched"] == 1
        assert out["files"][0]["filename"] == "raw_meas.mzML"

    def test_filter_by_mime_contains(self):
        state = CrateState()
        state.scanned_files = [
            _fc("/d/a.csv", "a.csv", 1, "text/csv"),
            _fc("/d/b.png", "b.png", 2, "image/png"),
        ]
        out = list_scanned_files(state, mime_contains="image")
        assert out["matched"] == 1
        assert out["files"][0]["filename"] == "b.png"

    def test_pagination(self):
        out = list_scanned_files(self._state(10), offset=3, limit=4)
        assert out["total_scanned"] == 10
        assert out["matched"] == 10
        assert out["offset"] == 3
        assert out["limit"] == 4
        assert out["returned"] == 4
        assert [f["filename"] for f in out["files"]] == [
            f"file{i}.csv" for i in (3, 4, 5, 6)
        ]


class TestUpdateEntity:
    """Tests for the update_entity function."""

    def test_adds_and_replaces_fields_and_updates_completion(self, minimal_state):
        """update_entity adds new fields, replaces existing ones,
        and updates completion metadata on the entity."""
        state = minimal_state

        patch = {"title": "Updated Title", "identifier": "10.1234/example"}
        updated = update_entity(state, "inv_001", patch)

        assert updated.entity_id == "inv_001"
        assert updated.fields["title"] == "Updated Title"
        assert updated.fields["identifier"] == "10.1234/example"

        # Check completion was updated for patched fields
        title_fc = updated.get_field_status("title")
        assert title_fc is not None
        assert title_fc.status == "filled"
        assert title_fc.source == "llm"

        ident_fc = updated.get_field_status("identifier")
        assert ident_fc is not None
        assert ident_fc.status == "filled"
        assert ident_fc.source == "llm"

        # Verify original entity in state was also updated (not a copy)
        retrieved = state.get_entity("inv_001")
        assert retrieved is updated
        assert retrieved.fields["title"] == "Updated Title"

    def test_raises_value_error_for_nonexistent_entity(self, minimal_state):
        """update_entity raises ValueError when entity_id doesn't exist."""
        with pytest.raises(ValueError, match="not found"):
            update_entity(minimal_state, "nonexistent", {"title": "X"})


class TestRemoveEntity:
    """Tests for the remove_entity function."""

    def test_removes_entity_and_reports_it(self, minimal_state):
        """remove_entity removes the entity and REPORTS what it did.

        A bare boolean was the bug, not the interface: removing four Assays
        detached thirteen processes and the caller was told `True`. The report
        names what came loose so the caller can re-point it.
        """
        state = minimal_state
        assert state.get_entity("inv_001") is not None

        result = remove_entity(state, "inv_001")

        assert result["removed"] is True
        assert result["entity_id"] == "inv_001"
        assert state.get_entity("inv_001") is None

    def test_reports_not_removed_for_nonexistent_entity(self, minimal_state):
        """A missing entity is reported, not silently treated as a removal."""
        result = remove_entity(minimal_state, "nonexistent")
        assert result["removed"] is False


class TestReferentialIntegrity:
    """Issue #92: remove must not leave dangling {@id} references."""

    def _linked_state(self):
        state = CrateState()
        state.add_entity(_ent("inv_1", "Investigation", name="Inv"))
        state.add_entity(_ent("study_1", "Study", name="St", investigation_id="inv_1"))
        state.add_entity(_ent("assay_1", "Assay", name="As", study_id="study_1"))
        # a process consuming two samples (list-valued reference)
        state.add_entity(_ent("s1", "Sample", name="s1"))
        state.add_entity(_ent("s2", "Sample", name="s2"))
        state.add_entity(
            _ent("proc_1", "LabProcess", process_type="EndpointReadout",
                 assay_id="assay_1", samples=["s1", "s2"])
        )
        return state

    def test_find_referrers_scalar_and_list(self):
        state = self._linked_state()
        # study_1 is referenced by assay_1 via study_id
        refs = find_referrers(state, "study_1")
        assert ("assay_1", "study_id") in {(e.entity_id, f) for e, f in refs}
        # s1 is referenced by proc_1 via the list-valued samples field
        refs_s1 = find_referrers(state, "s1")
        assert ("proc_1", "samples") in {(e.entity_id, f) for e, f in refs_s1}

    def test_find_referrers_none_for_leaf(self):
        state = self._linked_state()
        assert find_referrers(state, "s2") == [] or all(
            e.entity_id != "s2" for e, _ in find_referrers(state, "proc_1")
        )
        # proc_1 is referenced by nothing
        assert find_referrers(state, "proc_1") == []

    def test_remove_referenced_entity_refuses_with_actionable_error(self):
        state = self._linked_state()
        with pytest.raises(ValueError, match="assay_1"):
            remove_entity(state, "study_1")
        # entity is NOT removed when the call is refused
        assert state.get_entity("study_1") is not None

    def test_remove_unreferenced_entity_succeeds(self):
        state = self._linked_state()
        result = remove_entity(state, "proc_1")
        assert result["removed"] is True
        # Nothing referenced it, so nothing came loose.
        assert result["detached"] == []
        assert state.get_entity("proc_1") is None

    def test_cascade_clears_scalar_reference(self):
        state = self._linked_state()
        result = remove_entity(state, "study_1", cascade=True)
        assert result["removed"] is True
        # THE point of the report: the assay lost its parent and says so.
        assert any("assay_1" in d for d in result["detached"]), result["detached"]
        assert result["warning"], "a detaching cascade must warn"
        assert state.get_entity("study_1") is None
        # assay_1's study_id is gone (no dangling ref)
        assert "study_id" not in state.get_entity("assay_1").fields

    def test_cascade_prunes_from_list_reference(self):
        state = self._linked_state()
        assert remove_entity(state, "s1", cascade=True)["removed"] is True
        # s2 survives in the samples list; s1 is pruned
        assert state.get_entity("proc_1").fields["samples"] == ["s2"]

    def test_no_dangling_id_in_built_graph_after_cascade(self, tmp_path):
        state = self._linked_state()
        # add a File the process produces, then remove the File with cascade
        state.add_entity(_ent("f_raw", "File", name="raw.csv", dest_path="raw.csv"))
        set_entity_field(state, "proc_1", "result", "f_raw")
        remove_entity(state, "f_raw", cascade=True)
        out = tmp_path / "crate"
        result = build_crate(state, str(out))
        assert result["success"] is True
        with open(out / "ro-crate-metadata.json") as fh:
            graph = json.load(fh)["@graph"]
        ids = {e.get("@id") for e in graph}
        # the removed File must not be referenced anywhere in the built graph
        blob = json.dumps(graph)
        assert "f_raw" not in blob and "#File_f_raw" not in ids

    def test_rename_via_name_keeps_entity_id_and_links(self):
        # "Renaming" is done by changing the name field; entity_id stays stable,
        # so referrers (which point at entity_id) are never orphaned (#92).
        state = self._linked_state()
        update_entity(state, "study_1", {"name": "Renamed Study"})
        assert state.get_entity("study_1") is not None  # entity_id unchanged
        # the assay still references the (same-id) study
        assert state.get_entity("assay_1").fields["study_id"] == "study_1"


class TestListEntities:
    """Tests for the list_entities function."""

    def test_returns_all_entities_when_no_type_given(self, state_with_multiple_entities):
        """list_entities returns all entities when entity_type is None."""
        entities = list_entities(state_with_multiple_entities)
        assert len(entities) == 3
        ids = {e.entity_id for e in entities}
        assert ids == {"inv_001", "stu_001", "chem_001"}

    def test_filters_by_entity_type(self, state_with_multiple_entities):
        """list_entities filters to only entities of the given type."""
        entities = list_entities(state_with_multiple_entities, entity_type="Investigation")
        assert len(entities) == 1
        assert entities[0].entity_id == "inv_001"
        assert entities[0].type == "Investigation"

    def test_returns_empty_list_for_unpopulated_type(self, state_with_multiple_entities):
        """list_entities returns empty list for a type with no entities."""
        entities = list_entities(state_with_multiple_entities, entity_type="Assay")
        assert entities == []


class TestSetEntityField:
    """Tests for the set_entity_field function."""

    def test_sets_a_single_field_with_correct_completion_tracking(self, minimal_state):
        """set_entity_field sets a field value and marks completion with given source."""
        state = minimal_state

        set_entity_field(state, "inv_001", "identifier", "10.1234/test", source="user")

        entity = state.get_entity("inv_001")
        assert entity.fields["identifier"] == "10.1234/test"

        fc = entity.get_field_status("identifier")
        assert fc is not None
        assert fc.status == "filled"
        assert fc.source == "user"

    def test_raises_value_error_for_nonexistent_entity(self, minimal_state):
        """set_entity_field raises ValueError when entity doesn't exist."""
        with pytest.raises(ValueError, match="not found"):
            set_entity_field(minimal_state, "nonexistent", "field", "value")


class TestBulkSetFields:
    """Tests for the bulk_set_fields function."""

    def test_sets_multiple_fields_at_once(self, minimal_state):
        """bulk_set_fields sets multiple fields and marks each as filled."""
        state = minimal_state

        bulk_set_fields(
            state,
            "inv_001",
            {"title": "New Title", "description": "New desc", "identifier": "id-123"},
            source="lookup",
        )

        entity = state.get_entity("inv_001")
        assert entity.fields["title"] == "New Title"
        assert entity.fields["description"] == "New desc"
        assert entity.fields["identifier"] == "id-123"

        for field in ("title", "description", "identifier"):
            fc = entity.get_field_status(field)
            assert fc is not None, f"Missing completion for {field}"
            assert fc.status == "filled", f"Status not filled for {field}"
            assert fc.source == "lookup", f"Source not lookup for {field}"

    def test_raises_value_error_for_nonexistent_entity(self, minimal_state):
        """bulk_set_fields raises ValueError when entity doesn't exist."""
        with pytest.raises(ValueError, match="not found"):
            bulk_set_fields(minimal_state, "nonexistent", {"f": "v"})


class TestSetFields:
    """Tests for the consolidated set_fields function (Issue #90, sub-task 2)."""

    def test_sets_multiple_fields_and_tracks_completion(self, minimal_state):
        """set_fields sets every field and marks each filled with the source."""
        from builder.tools.management import set_fields

        entity = set_fields(
            minimal_state,
            "inv_001",
            {"title": "T", "identifier": "id-1"},
            source="lookup",
        )

        assert entity.entity_id == "inv_001"
        assert entity.fields["title"] == "T"
        assert entity.fields["identifier"] == "id-1"
        for field in ("title", "identifier"):
            fc = entity.get_field_status(field)
            assert fc is not None and fc.status == "filled" and fc.source == "lookup"

    def test_single_field_is_just_a_one_key_dict(self, minimal_state):
        """The single-field case is the one-entry dict — no separate tool needed."""
        from builder.tools.management import set_fields

        set_fields(minimal_state, "inv_001", {"identifier": "10.1/x"})
        assert minimal_state.get_entity("inv_001").fields["identifier"] == "10.1/x"

    def test_returns_the_updated_entity(self, minimal_state):
        from builder.tools.management import set_fields

        result = set_fields(minimal_state, "inv_001", {"title": "X"})
        assert result is minimal_state.get_entity("inv_001")

    def test_raises_value_error_for_nonexistent_entity(self, minimal_state):
        from builder.tools.management import set_fields

        with pytest.raises(ValueError, match="not found"):
            set_fields(minimal_state, "nonexistent", {"f": "v"})


class TestConsolidatedMutationTool:
    """The three redundant mutation tools collapse into one registered tool."""

    def test_only_set_fields_is_registered_not_the_redundant_three(self):
        import builder.tools.management  # noqa: F401  (triggers registration)
        from builder.tools.registry import TOOL_REGISTRY

        names = set(TOOL_REGISTRY.list())
        assert "set_fields" in names
        for redundant in ("update_entity", "bulk_set_fields", "set_entity_field"):
            assert redundant not in names, f"{redundant} should no longer be registered"

    def test_set_fields_is_in_tool_specs_and_redundant_ones_are_not(self):
        from builder.agents.react.tools_spec import TOOL_SPECS

        names = {s["name"] for s in TOOL_SPECS}
        assert "set_fields" in names
        for redundant in ("update_entity", "bulk_set_fields", "set_entity_field"):
            assert redundant not in names


class TestTheAccessionIsJudgedOnShape:
    """#628: the crate carries the accession as ``schema:identifier``, where
    every consumer reads it as something to cite."""

    def test_a_slug_is_recorded_but_said_out_loud(self, caplog) -> None:
        """Recorded, because refusing would drop the only identifier the crate
        has and this tool cannot tell a weak one from a real one it has never
        heard of. Said out loud, because a title slugged into that field
        identifies nothing."""
        from builder.tools.management import set_crate_metadata

        state = CrateState()
        slug = "inv_neural_cell_screening_models_for_endocrine_disruption_of_thyroid"
        with caplog.at_level(logging.WARNING, logger="builder.tools.management"):
            set_crate_metadata(state, accession=slug)

        assert state.metadata.accession == slug
        assert any("does not read as an identifier" in r.message for r in caplog.records)

    def test_a_real_accession_passes_without_comment(self, caplog) -> None:
        from builder.tools.management import set_crate_metadata

        state = CrateState()
        with caplog.at_level(logging.WARNING, logger="builder.tools.management"):
            set_crate_metadata(state, accession="S-VHPS21")

        assert state.metadata.accession == "S-VHPS21"
        assert not caplog.records

    def test_a_doi_passes_without_comment(self, caplog) -> None:
        """Long, but one token and citable — the rule is not merely "short"."""
        from builder.tools.management import set_crate_metadata

        state = CrateState()
        doi = "https://doi.org/10.1007/s00204-024-03787-2"
        with caplog.at_level(logging.WARNING, logger="builder.tools.management"):
            set_crate_metadata(state, accession=doi)

        assert not caplog.records
