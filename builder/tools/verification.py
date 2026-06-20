"""Identifier verification tools for the ISA-Tox RO-Crate Builder.

Verification checks that identifiers resolve at their source by attempting
to look them up. For now, all verifications are stubs that return
unverified with an appropriate message.
"""

from __future__ import annotations

from builder.state import CrateState


def verify_identifier(state: CrateState, entity_id: str, field: str) -> dict:
    """Check that an identifier resolves at its source.

    For now, this is a stub that returns verified=False with a message
    indicating verification is not yet implemented.

    Args:
        state: The crate state containing the entity.
        entity_id: ID of the entity whose field to verify.
        field: The field name to verify.

    Returns:
        A dict with keys: verified (bool), entity_id (str), field (str),
        message (str), suggested_fix (str | None).
    """
    entity = state.get_entity(entity_id)
    if entity is None:
        return {
            "verified": False,
            "entity_id": entity_id,
            "field": field,
            "message": f"Entity not found: {entity_id}",
            "suggested_fix": "Ensure the entity exists before verifying its fields.",
        }

    return {
        "verified": False,
        "entity_id": entity_id,
        "field": field,
        "message": f"Verification not yet implemented for {field} on {entity.type}",
        "suggested_fix": None,
    }


def verify_all_identifiers(state: CrateState) -> list[dict]:
    """Run verify_identifier on every entity field marked as 'filled'.

    Iterates over all entities in the state and checks each field that
    has completion status "filled".

    Returns:
        A list of verification result dicts (one per filled field).
    """
    results: list[dict] = []

    for entity in state.list_entities():
        for comp_key, fc in entity._completion.items():
            if fc.status == "filled":
                # comp_key is "{type}:{field}" — extract the field name
                field = comp_key.split(":", 1)[1]
                result = verify_identifier(state, entity.entity_id, field)
                results.append(result)

    return results