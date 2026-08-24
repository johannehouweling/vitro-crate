"""Identifier verification tools for the ISA-Tox RO-Crate Builder."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from builder.state import CrateState
from builder.tools.lookups import (
    cell_line_names_match,
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


# ---------------------------------------------------------------------------
# Single source of truth for verifiable (entity_type, field) pairs.
#
# The dispatch table IS the authoritative set: a pair cannot be declared
# verifiable without naming the lookup that serves it. ``verify_all_identifiers``
# decides what to queue from these keys, and ``verify_identifier`` dispatches
# through the same table — so the two cannot drift apart (#64).
#
# The table holds the lookup functions themselves, so the dependency stays
# visible to the linter and the type checker. ``_select_verifier`` re-resolves
# each through the module namespace by name before returning it, so patching
# ``builder.tools.verification.lookup_*`` still takes effect.
# ---------------------------------------------------------------------------
_VERIFIERS: dict[tuple[str, str], tuple[Callable[[str], dict], str]] = {
    # MolecularEntity resolves through PubChem, except dtxsid (EPA CompTox)
    ("MolecularEntity", "identifier"): (lookup_compound, "pubchem"),
    ("MolecularEntity", "cas"): (lookup_compound, "pubchem"),
    ("MolecularEntity", "casrn"): (lookup_compound, "pubchem"),
    ("MolecularEntity", "cas_number"): (lookup_compound, "pubchem"),
    ("MolecularEntity", "pubchem_cid"): (lookup_compound, "pubchem"),
    ("MolecularEntity", "inchikey"): (lookup_compound, "pubchem"),
    ("MolecularEntity", "dtxsid"): (lookup_dtxsid, "comptox"),
    # CellLineSample resolves through Cellosaurus
    ("CellLineSample", "identifier"): (lookup_cell_line, "cellosaurus"),
    ("CellLineSample", "accession"): (lookup_cell_line, "cellosaurus"),
    # Person resolves through ORCID
    ("Person", "identifier"): (lookup_orcid, "orcid"),
    ("Person", "orcid"): (lookup_orcid, "orcid"),
    # Publication resolves through Crossref
    ("Publication", "identifier"): (lookup_doi, "crossref"),
    ("Publication", "doi"): (lookup_doi, "crossref"),
}

_VERIFIABLE_FIELDS: frozenset[tuple[str, str]] = frozenset(_VERIFIERS)


def _select_verifier(entity_type: str, field: str):
    """Return the lookup function and service name for an identifier field.

    Returns ``(None, None)`` when the pair is not verifiable. The pair must be
    a key of :data:`_VERIFIERS`, which is also what :data:`_VERIFIABLE_FIELDS`
    is built from — so a pair this returns nothing for is never queued by
    ``verify_all_identifiers`` in the first place.
    """
    entry = _VERIFIERS.get((entity_type, field.lower()))
    if entry is None:
        return None, None
    verifier, lookup_name = entry
    # Re-resolve through the module namespace so a patched
    # ``builder.tools.verification.lookup_*`` is honoured.
    name = getattr(verifier, "__name__", "")
    return globals().get(name, verifier), lookup_name


def _get_verifiable_fields() -> frozenset[tuple[str, str]]:
    """Return the (entity_type, field) pairs that have a verifier.

    The single source of truth for which identifier fields can be
    auto-verified: the keys of :data:`_VERIFIERS`, which ``_select_verifier``
    dispatches through.
    """
    return _VERIFIABLE_FIELDS


def _cell_line_mismatch(entity, field: str, result: dict, lookup_name: str | None) -> dict | None:
    """Reject a resolving accession that names a *different* cell line (#383).

    Existence is not identity. ``lookup_cell_line`` answers "is this a real
    Cellosaurus record?", but the D5 question is "is it *this* entity's record?"
    — and a model that could not resolve a name has every incentive to reach for
    the nearest concrete accession in its context. Since the resolved record
    already carries its own name, the cross-check costs no extra call.

    Returns a verdict dict when the record demonstrably names something else, or
    ``None`` to let verification proceed (matched, or nothing to compare
    against).

    The value is deliberately **kept** on a mismatch. An engineered derivative
    (``"CHO-K1 hOATP1C1"`` against parent record ``"CHO-K1"``) is a correct
    accession that simply cannot be name-matched, so clearing would destroy good
    data. Withholding the false ``verified`` claim is what fixes the D5
    violation; deleting the value would only trade it for a worse one.
    """
    if entity.type != "CellLineSample":
        return None
    own_name = str(entity.fields.get("name") or "").strip()
    if not own_name:
        # Nothing to compare against — existence is all we can assert.
        return None

    data = result.get("data") or {}
    resolved_name = str(data.get("name") or "").strip()
    if not resolved_name:
        return None

    synonyms = data.get("alternateName") or []
    if isinstance(synonyms, str):
        synonyms = [synonyms]
    if cell_line_names_match(own_name, resolved_name, synonyms):
        return None

    return {
        "verified": False,
        "mismatch": True,
        "entity_id": entity.entity_id,
        "field": field,
        "resolved_name": resolved_name,
        "message": (
            f"{field} '{entity.fields.get(field)}' resolves at "
            f"{lookup_name or 'the source'} to '{resolved_name}', which does not "
            f"match this entity's name '{own_name}'; value kept but NOT marked "
            "verified."
        ),
        "suggested_fix": (
            f"Confirm the accession belongs to '{own_name}' — resolve it with "
            "lookup_cell_line_by_name, or, if this is an engineered derivative of "
            f"'{resolved_name}', record that relationship explicitly rather than "
            "reusing the parent's accession as this sample's identifier."
        ),
    }


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
        # Resolving is necessary but not sufficient: the record must also be
        # this entity's record (#383).
        mismatch = _cell_line_mismatch(entity, field, result, lookup_name)
        if mismatch is not None:
            return mismatch
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
