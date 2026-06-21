"""Identifier verification tools for the ISA-Tox RO-Crate Builder."""

from __future__ import annotations

from builder.state import CrateState
from builder.tools.lookups import (
    lookup_cell_line,
    lookup_compound,
    lookup_doi,
    lookup_orcid,
)


def _select_verifier(entity_type: str, field: str):
    """Return an appropriate lookup function for an identifier-like field."""
    field_name = field.lower()
    if entity_type == "MolecularEntity" and field_name in {
        "identifier",
        "cas",
        "casrn",
        "cas_number",
        "pubchem_cid",
        "inchikey",
    }:
        return lookup_compound, "pubchem"
    if entity_type == "CellLineSample" and field_name in {"identifier", "accession"}:
        return lookup_cell_line, "cellosaurus"
    if entity_type == "Person" and field_name in {"identifier", "orcid"}:
        return lookup_orcid, "orcid"
    if entity_type == "Publication" and field_name in {"identifier", "doi"}:
        return lookup_doi, "crossref"
    return None, None


def verify_identifier(state: CrateState, entity_id: str, field: str) -> dict:
    """Check that an identifier resolves at its source.

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

    value = entity.fields.get(field)
    if value in (None, ""):
        return {
            "verified": False,
            "entity_id": entity_id,
            "field": field,
            "message": f"No value present for {field} on {entity.type}",
            "suggested_fix": "Provide an identifier value before verification.",
        }

    verifier, lookup_name = _select_verifier(entity.type, field)
    if verifier is None:
        return {
            "verified": False,
            "entity_id": entity_id,
            "field": field,
            "message": f"No verifier configured for {field} on {entity.type}",
            "suggested_fix": None,
        }

    query = f"CID {value}" if field.lower() == "pubchem_cid" else str(value)
    result = verifier(query)
    if result.get("found"):
        entity.set_field_status(field, "verified", "lookup")
        if lookup_name and lookup_name not in entity._provenance.lookups_used:
            entity._provenance.lookups_used.append(lookup_name)
        return {
            "verified": True,
            "entity_id": entity_id,
            "field": field,
            "message": f"Verified {field} for {entity.type} via {lookup_name}",
            "suggested_fix": None,
        }

    # Transient lookup failure (timeout / 429 / 5xx): the source could not be
    # reached, which is NOT evidence the identifier is wrong. Keep the user's
    # value intact (status stays "filled") instead of destroying it.
    if result.get("transient"):
        return {
            "verified": False,
            "entity_id": entity_id,
            "field": field,
            "message": (
                f"{field} for {entity.type} could not be verified right now — "
                f"{lookup_name or 'the source'} is temporarily unavailable; "
                "value kept."
            ),
            "suggested_fix": "Retry verification later.",
        }

    entity.fields.pop(field, None)
    entity.set_field_status(field, "missing", "lookup")
    return {
        "verified": False,
        "entity_id": entity_id,
        "field": field,
        "message": (
            f"{field} could not be verified for {entity.type}; "
            "value cleared from entity."
        ),
        "suggested_fix": "Provide a resolvable identifier and verify again.",
    }


_IDENTIFIER_FIELDS = {
    "identifier",
    "cas",
    "orcid",
    "ror",
    "doi",
    "accession",
    "pubchem_cid",
}


def verify_all_identifiers(state: CrateState) -> list[dict]:
    """Run verify_identifier on every identifier field marked as 'filled'.

    Iterates over all entities in the state and checks each field whose
    name is an identifier-like field (e.g. identifier, cas, orcid, ror,
    doi, accession, pubchem_cid) with completion status "filled".

    Returns:
        A list of verification result dicts (one per qualifying filled field).
    """
    results: list[dict] = []

    for entity in state.list_entities():
        for comp_key, fc in entity._completion.items():
            if fc.status == "filled":
                # comp_key is "{type}:{field}" — extract the field name
                field = comp_key.split(":", 1)[1]
                if field not in _IDENTIFIER_FIELDS:
                    continue
                result = verify_identifier(state, entity.entity_id, field)
                results.append(result)

    return results


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("verify_identifier", verify_identifier, takes_state=True)
TOOL_REGISTRY.register(
    "verify_all_identifiers", verify_all_identifiers, takes_state=True
)