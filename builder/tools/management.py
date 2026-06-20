"""Entity management tools for the ISA-Tox RO-Crate Builder.

Provides CRUD operations on entities within a CrateState, including
field-level completion tracking.
"""

from __future__ import annotations

from typing import Any

from builder.state import (
    CompletionSource,
    CrateState,
    Entity,
)


def update_entity(state: CrateState, entity_id: str, patch: dict) -> Entity:
    """Update fields on an existing entity.

    Adds new fields, replaces existing ones, and updates completion
    metadata for each patched field.

    Args:
        state: The crate state to operate on.
        entity_id: The ID of the entity to update.
        patch: Dictionary of field names to new values.

    Returns:
        The updated Entity.

    Raises:
        ValueError: If no entity with the given ID exists.
    """
    entity = state.get_entity(entity_id)
    if entity is None:
        raise ValueError(f"Entity not found: {entity_id}")

    for field, value in patch.items():
        entity.fields[field] = value
        entity.set_field_status(field, "filled", "llm")

    return entity


def remove_entity(state: CrateState, entity_id: str) -> bool:
    """Remove an entity by id.

    Args:
        state: The crate state to operate on.
        entity_id: The ID of the entity to remove.

    Returns:
        True if the entity was found and removed, False otherwise.
    """
    return state.remove_entity(entity_id)


def list_entities(state: CrateState, entity_type: str | None = None) -> list[Entity]:
    """List entities, optionally filtered by type.

    Args:
        state: The crate state to query.
        entity_type: Optional entity type to filter by (e.g. "Investigation").
            If None, all entities are returned.

    Returns:
        A list of matching Entity objects.
    """
    return state.list_entities(entity_type=entity_type)


def set_entity_field(
    state: CrateState,
    entity_id: str,
    field: str,
    value: Any,
    source: CompletionSource = "llm",
) -> None:
    """Set a single field on an entity and update its completion tracking.

    Args:
        state: The crate state to operate on.
        entity_id: The ID of the entity to update.
        field: The field name to set.
        value: The value to assign.
        source: The provenance source for this field value.

    Raises:
        ValueError: If no entity with the given ID exists.
    """
    entity = state.get_entity(entity_id)
    if entity is None:
        raise ValueError(f"Entity not found: {entity_id}")

    entity.fields[field] = value
    entity.set_field_status(field, "filled", source)


def bulk_set_fields(
    state: CrateState,
    entity_id: str,
    fields: dict[str, Any],
    source: CompletionSource = "llm",
) -> None:
    """Set multiple fields at once on an entity.

    Args:
        state: The crate state to operate on.
        entity_id: The ID of the entity to update.
        fields: Dictionary of field names to values.
        source: The provenance source for these field values.

    Raises:
        ValueError: If no entity with the given ID exists.
    """
    entity = state.get_entity(entity_id)
    if entity is None:
        raise ValueError(f"Entity not found: {entity_id}")

    for field, value in fields.items():
        entity.fields[field] = value
        entity.set_field_status(field, "filled", source)