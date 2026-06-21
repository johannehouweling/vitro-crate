"""Shared test fixtures for the builder test suite."""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance


@pytest.fixture
def minimal_state() -> CrateState:
    """Return a CrateState with one Investigation entity."""
    state = CrateState()
    entity = Entity(
        entity_id="inv_001",
        type="Investigation",
        fields={"title": "Test Investigation", "description": "A test"},
        _provenance=EntityProvenance(created_by="llm"),
    )
    state.add_entity(entity)
    return state


@pytest.fixture
def state_with_multiple_entities() -> CrateState:
    """Return a CrateState with entities of different types for filtering tests."""
    state = CrateState()

    inv = Entity(
        entity_id="inv_001",
        type="Investigation",
        fields={"title": "My Investigation"},
        _provenance=EntityProvenance(created_by="llm"),
    )
    state.add_entity(inv)

    study = Entity(
        entity_id="stu_001",
        type="Study",
        fields={"title": "My Study"},
        _provenance=EntityProvenance(created_by="llm"),
    )
    state.add_entity(study)

    mol = Entity(
        entity_id="chem_001",
        type="MolecularEntity",
        fields={"name": "Test Compound"},
        _provenance=EntityProvenance(created_by="lookup"),
    )
    state.add_entity(mol)

    return state
