"""Identifier verification tools for the ISA-Tox RO-Crate Builder."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from builder.state import CrateState
from builder.tools.lookups import (
    lookup_cell_line,
    lookup_compound,
    lookup_doi,
    lookup_dtxsid,
    lookup_orcid,
)

# Bound on concurrent identifier verifications. Each verification is an
# independent network lookup with no data dependency on the others, so they run
# in a bounded pool; per-host politeness is enforced by the throttle in
# lookups._http. The lookups themselves are lru_cache'd, so this only helps the
# cold path (#62).
_VERIFY_WORKERS = 6


def _select_verifier(entity_type: str, field: str):
    """Return an appropriate lookup function for an identifier-like field."""
    field_name = field.lower()
    if entity_type == "MolecularEntity" and field_name == "dtxsid":
        return lookup_dtxsid, "comptox"
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


# ---------------------------------------------------------------------------
# Single source of truth for verifiable (entity_type, field) pairs.
# This is the *authoritative* set — both _select_verifier and
# verify_all_identifiers derive from it, so they can never drift apart.
# ---------------------------------------------------------------------------

_VERIFIABLE_FIELDS: frozenset[tuple[str, str]] = frozenset(
    [
        # MolecularEntity fields that map to PubChem lookup
        ("MolecularEntity", "identifier"),
        ("MolecularEntity", "cas"),
        ("MolecularEntity", "casrn"),
        ("MolecularEntity", "cas_number"),
        ("MolecularEntity", "pubchem_cid"),
        ("MolecularEntity", "inchikey"),
        # MolecularEntity dtxsid maps to the EPA CompTox lookup
        ("MolecularEntity", "dtxsid"),
        # CellLineSample fields that map to Cellosaurus lookup
        ("CellLineSample", "identifier"),
        ("CellLineSample", "accession"),
        # Person fields that map to ORCID lookup
        ("Person", "identifier"),
        ("Person", "orcid"),
        # Publication fields that map to Crossref lookup
        ("Publication", "identifier"),
        ("Publication", "doi"),
    ]
)


def _get_verifiable_fields() -> frozenset[tuple[str, str]]:
    """Return the set of (entity_type, field) pairs that have a verifier.

    This is the single source of truth for which identifier fields can be
    auto-verified. Both ``verify_all_identifiers`` and ``_select_verifier``
    are derived from this set.
    """
    return _VERIFIABLE_FIELDS


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
        "message": (f"{field} could not be verified for {entity.type}; value cleared from entity."),
        "suggested_fix": "Provide a resolvable identifier and verify again.",
    }


# Legacy re-export — derived automatically from _get_verifiable_fields so it
# always stays in sync. Only the flat field names are exposed here; the
# authoritative pair-based set is _get_verifiable_fields().
_IDENTIFIER_FIELDS: frozenset[str] = frozenset({f for (_t, f) in _get_verifiable_fields()})


def verify_all_identifiers(state: CrateState) -> list[dict]:
    """Run verify_identifier on every verifiable field marked as 'filled'.

    Iterates over all entities in the state and checks each (entity_type, field)
    pair that has a verifier configured, if the field's completion status is
    "filled".

    Unlike the previous implementation that used a flat hard-coded field-name
    set, this version queries ``_get_verifiable_fields()`` — the single source
    of truth shared with ``_select_verifier`` — so it never misses fields like
    ``casrn``/``cas_number``/``inchikey`` on MolecularEntity, and never attempts
    ``ror`` on Organization (which has no verifier).

    The independent per-field lookups are dispatched concurrently with a bounded
    thread pool (#62). The work plan is collected deterministically first and the
    results are returned in that same order, so the output is identical to the
    serial path regardless of which lookup completes first; per-host politeness
    is enforced by the throttle in ``lookups._http``.

    Returns:
        A list of verification result dicts (one per qualifying filled field).
    """
    verifiable = _get_verifiable_fields()

    # Collect the work plan deterministically (entity order, then completion-key
    # order) so the returned results never depend on thread scheduling.
    tasks: list[tuple[str, str]] = []
    for entity in state.list_entities():
        for comp_key, fc in entity._completion.items():
            if fc.status == "filled":
                # comp_key is "{type}:{field}" — extract the field name
                field = comp_key.split(":", 1)[1]
                if (entity.type, field) not in verifiable:
                    continue
                tasks.append((entity.entity_id, field))

    if not tasks:
        return []

    # Each task verifies a distinct (entity, field) pair, mutating only that
    # field's status — no shared-state contention between tasks.
    with ThreadPoolExecutor(max_workers=min(_VERIFY_WORKERS, len(tasks))) as pool:
        results = list(
            pool.map(
                lambda task: verify_identifier(state, task[0], task[1]),
                tasks,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("verify_identifier", verify_identifier, takes_state=True)
TOOL_REGISTRY.register("verify_all_identifiers", verify_all_identifiers, takes_state=True)
