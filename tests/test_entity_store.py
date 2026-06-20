"""Tests for the EntityStore helper in builder.state."""

from __future__ import annotations

from typing import Any

import pytest

from builder.state import CrateState, Entity, EntityProvenance, EntityStore, EntityType


def _entity(entity_id: str, entity_type: EntityType, **fields: Any) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=entity_type,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


class TestEntityStore:
    """Focused CRUD tests for EntityStore."""

    def test_empty_store_returns_no_entities(self):
        store = EntityStore()

        assert store.get_entity("missing") is None
        assert store.remove_entity("missing") is False
        assert store.list_entities() == []
        assert store.list_entities("Investigation") == []

    def test_add_get_and_filter_entities(self):
        store = EntityStore()
        inv = _entity("inv_001", "Investigation", title="Investigation")
        study = _entity("stu_001", "Study", title="Study")
        chem = _entity("chem_001", "MolecularEntity", name="Compound")

        store.add_entity(inv)
        store.add_entity(study)
        store.add_entity(chem)

        assert store.get_entity("inv_001") is inv
        assert store.get_entity("stu_001") is study
        assert store.get_entity("chem_001") is chem
        assert {entity.entity_id for entity in store.list_entities()} == {
            "inv_001",
            "stu_001",
            "chem_001",
        }
        assert store.list_entities("Study") == [study]

    def test_cell_line_sample_stored_in_samples_collection(self):
        store = EntityStore()
        sample = _entity("sample_001", "Sample", name="Sample")
        cell_line = _entity("cell_001", "CellLineSample", name="HepG2")

        store.add_entity(sample)
        store.add_entity(cell_line)

        assert store.samples == {
            "sample_001": sample,
            "cell_001": cell_line,
        }
        assert {entity.entity_id for entity in store.list_entities()} == {
            "sample_001",
            "cell_001",
        }
        assert store.list_entities("CellLineSample") == [cell_line]

    def test_remove_entity_removes_from_underlying_collection(self):
        store = EntityStore()
        person = _entity("person_001", "Person", name="Jane Doe")
        store.add_entity(person)

        assert store.remove_entity("person_001") is True
        assert store.people == {}
        assert store.get_entity("person_001") is None

    def test_unknown_entity_type_raises_value_error(self):
        store = EntityStore()

        with pytest.raises(ValueError, match="Unknown entity type"):
            store.list_entities("UnknownType")


class TestCrateStateDelegation:
    """CrateState should delegate CRUD work to its EntityStore."""

    def test_add_and_remove_entity_delegate_to_entity_store(self):
        state = CrateState()
        entity = _entity("org_001", "Organization", name="Example Org")

        state.add_entity(entity)

        assert state.entities.organizations == {"org_001": entity}
        assert state.get_entity("org_001") is entity

        assert state.remove_entity("org_001") is True
        assert state.entities.organizations == {}
