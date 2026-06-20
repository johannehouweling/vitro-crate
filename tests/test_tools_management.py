"""Tests for builder/tools/management.py."""

from __future__ import annotations

import pytest

from builder.state import Entity
from builder.tools.management import (
    bulk_set_fields,
    list_entities,
    remove_entity,
    set_entity_field,
    update_entity,
)


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

    def test_removes_entity_and_returns_true(self, minimal_state):
        """remove_entity removes the entity from state and returns True."""
        state = minimal_state
        assert state.get_entity("inv_001") is not None

        result = remove_entity(state, "inv_001")

        assert result is True
        assert state.get_entity("inv_001") is None

    def test_returns_false_for_nonexistent_entity(self, minimal_state):
        """remove_entity returns False when the entity doesn't exist."""
        result = remove_entity(minimal_state, "nonexistent")
        assert result is False


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