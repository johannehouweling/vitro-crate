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
from builder.tools._crate_mapping import _REF_FIELDS


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


def _ref_key(value: Any) -> str | None:
    """The bare entity_id a single reference value points at (``#`` stripped)."""
    key = value.get("@id") if isinstance(value, dict) else value
    return key.lstrip("#") if isinstance(key, str) else None


def find_referrers(state: CrateState, entity_id: str) -> list[tuple[Entity, str]]:
    """Find every entity that references ``entity_id`` through a reference field.

    Scans each entity's reference-bearing fields (``_REF_FIELDS`` — the same set
    the crate mapping resolves) for a pointer to ``entity_id``, handling scalar
    values, ``{"@id": ...}`` objects, and lists thereof.

    Args:
        state: The crate state to scan.
        entity_id: The entity_id to find referrers of.

    Returns:
        ``(referrer_entity, field_name)`` tuples — one per field that points at
        ``entity_id``. The entity itself is never reported as its own referrer.
    """
    referrers: list[tuple[Entity, str]] = []
    for ent in state.list_entities():
        if ent.entity_id == entity_id:
            continue
        for field in _REF_FIELDS:
            value = ent.fields.get(field)
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            if any(_ref_key(item) == entity_id for item in items):
                referrers.append((ent, field))
    return referrers


def _drop_reference(fields: dict[str, Any], field: str, entity_id: str) -> None:
    """Remove every pointer to ``entity_id`` from ``fields[field]`` in place."""
    value = fields.get(field)
    if isinstance(value, list):
        pruned = [item for item in value if _ref_key(item) != entity_id]
        if pruned:
            fields[field] = pruned
        else:
            del fields[field]
    elif _ref_key(value) == entity_id:
        del fields[field]


def remove_entity(state: CrateState, entity_id: str, cascade: bool = False) -> bool:
    """Remove an entity by id, preserving referential integrity.

    The builder rebuilds the crate from state on every iteration, so a dangling
    reference left in state surfaces as a dangling ``{"@id": ...}`` in the built
    ``ro-crate-metadata.json``. To prevent that, removal first finds every
    referrer:

    - ``cascade=False`` (default): if any entity still references the target,
      refuse with an actionable ``ValueError`` naming the referrers (and the
      ``cascade=True`` escape hatch). The target is left in place.
    - ``cascade=True``: clear the target's id out of every referrer's field
      first, then remove it — no dangling references survive.

    Args:
        state: The crate state to operate on.
        entity_id: The ID of the entity to remove.
        cascade: When True, clear referrers instead of refusing.

    Returns:
        True if the entity was found and removed, False otherwise.

    Raises:
        ValueError: If the entity is still referenced and ``cascade`` is False.
    """
    referrers = find_referrers(state, entity_id)
    if referrers and not cascade:
        named = ", ".join(
            sorted({f"{ent.entity_id} (via {field})" for ent, field in referrers})
        )
        raise ValueError(
            f"Cannot remove '{entity_id}': still referenced by {named}. "
            f"Repoint or remove those references first, or pass cascade=True to "
            f"clear them."
        )
    if cascade:
        for ent, field in referrers:
            _drop_reference(ent.fields, field, entity_id)
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


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("list_entities", list_entities, takes_state=True)
TOOL_REGISTRY.register("update_entity", update_entity, takes_state=True)
TOOL_REGISTRY.register("remove_entity", remove_entity, takes_state=True)
TOOL_REGISTRY.register("set_entity_field", set_entity_field, takes_state=True)
TOOL_REGISTRY.register("bulk_set_fields", bulk_set_fields, takes_state=True)
