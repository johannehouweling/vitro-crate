"""
Compound lookup against the VHP4Safety Compound Wiki.

The Compound Wiki is a Wikibase instance, so a compound is an item with a
``Q`` id and a dereferenceable IRI — ``.../entity/Q2026`` — which is what the
ISA-Tox profile asks a ``MolecularEntity``'s ``@id`` to be. Resolving a name
takes two calls: ``wbsearchentities`` for the id, then the item's JSON for its
claims.

Property numbers in Wikibase are instance-local: ``P10`` is the InChIKey on
*this* wiki and means nothing on any other. :data:`_CLAIMS` is therefore the
contract with this one instance, recorded against it rather than inferred from
the shape of a value, and ``tests/test_lookups_contract.py`` pins it.
"""

from __future__ import annotations

import functools

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

BASE = "https://compoundcloud.wikibase.cloud"
_SEARCH = f"{BASE}/w/api.php"
_ENTITY = f"{BASE}/wiki/Special:EntityData/{{}}.json"

# Instance-local property ids, resolved from their English labels on the wiki.
_CLAIMS = {
    "formula": "P3",  # chemical formula
    "inchi": "P9",  # InChI
    "inchikey": "P10",  # InChIKey
    "smiles": "P12",  # SMILES (without stereochemistry)
    "pubchem_cid": "P13",  # PubChem CID
    "dtxcid": "P22",  # DSSTOX compound identifier
    "cas": "P23",  # CAS Registry Number
}

_SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")


def _claim(claims: dict, prop: str) -> str:
    """The first string value of *prop*, or "" when the item does not state it."""
    for claim in claims.get(prop, ()):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, str) and value:
            return value
    return ""


@functools.lru_cache(maxsize=512)
def lookup_compound_wiki(name: str) -> dict:
    """Resolve a compound name to its Compound Wiki record.

    Args:
        name: plain-text compound name, e.g. "caffeine".

    Returns:
        dict with keys ``compoundwiki_id``, ``@id``, ``name``, and whichever of
        ``inchikey``, ``smiles``, ``inchi``, ``formula``, ``cas``,
        ``pubchem_cid`` and ``dtxcid`` the item states (absent ones are ""). The
        formula is normalised from the wiki's Unicode subscripts to the ASCII
        digits ``schema:molecularFormula`` expects. Returns {} when the name
        matches nothing. Raises TransientLookupError on a transient API failure.
    """
    if not name or not name.strip():
        return {}
    try:
        found = http_get_json(
            _SEARCH,
            params={
                "action": "wbsearchentities",
                "search": name.strip(),
                "language": "en",
                "type": "item",
                "format": "json",
                "limit": 1,
            },
        )
        if found is NOT_FOUND:
            return {}
        hits = (found or {}).get("search") or []
        if not hits:
            return {}
        qid = hits[0].get("id") or ""
        if not qid:
            return {}

        item = http_get_json(_ENTITY.format(qid))
        if item is NOT_FOUND:
            return {}
        entity = (item or {}).get("entities", {}).get(qid, {})
        claims = entity.get("claims", {})
        label = entity.get("labels", {}).get("en", {}).get("value") or hits[0].get(
            "label", ""
        )

        record = {
            "compoundwiki_id": qid,
            "@id": hits[0].get("concepturi") or f"{BASE}/entity/{qid}",
            "name": label,
        }
        record.update({key: _claim(claims, prop) for key, prop in _CLAIMS.items()})
        record["formula"] = record["formula"].translate(_SUBSCRIPTS)
        return record

    except TransientLookupError:
        raise
    except Exception:
        return {}
