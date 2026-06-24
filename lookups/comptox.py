"""
EPA CompTox Chemicals Dashboard lookup.

Resolves a chemical (by name, CAS RN, or InChIKey) to its EPA DSSTox
substance identifier (DTXSID) via the public CompTox Dashboard search API.
The DTXSID is the stable key into the EPA hazard/exposure datasets, so it is
the anchor identifier for the toxicology profile.

Follows the same shape as the other raw lookup modules (``http_get_json`` +
``lru_cache`` + return ``{}`` on failure, re-raise ``TransientLookupError``).
"""

from __future__ import annotations

import functools
from urllib.parse import quote

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

# Public CompTox Dashboard search endpoint (no API key required). An exact
# ("equal") match is tried first; the response is a list of candidate hits.
_BASE = "https://comptox.epa.gov/dashboard-api/ccdapp2/search/chemical/equal"


def _first_hit(data: object) -> dict | None:
    """Return the first chemical hit from a CompTox search response, or None.

    The endpoint returns a JSON array of hit objects; older deployments wrap
    the array in a top-level key, so handle both shapes defensively.
    """
    hits: list = []
    if isinstance(data, list):
        hits = data
    elif isinstance(data, dict):
        candidate = data.get("hits") or data.get("results") or data.get("content") or []
        if isinstance(candidate, list):
            hits = candidate
    for hit in hits:
        if isinstance(hit, dict) and hit.get("dtxsid"):
            return hit
    return None


@functools.lru_cache(maxsize=512)
def lookup_dtxsid(query: str) -> dict:
    """Resolve a chemical to its EPA DTXSID via the CompTox Dashboard.

    Args:
        query: a chemical name, CAS RN, or InChIKey, e.g. "Bisphenol A",
               "80-05-7", or "IISBACLAFKSPIT-UHFFFAOYSA-N".

    Returns:
        dict with keys: dtxsid, name, casrn, inchikey, @id (a resolvable
        DTXSID IRI), @type ("MolecularEntity"). Returns {} if no match. Raises
        TransientLookupError on a transient API failure (timeout / connection /
        429 / 5xx).
    """
    if not query:
        return {}
    try:
        data = http_get_json(f"{_BASE}/{quote(query.strip())}")
        if data is NOT_FOUND:
            return {}

        hit = _first_hit(data)
        if hit is None:
            return {}

        dtxsid = hit.get("dtxsid", "")
        if not dtxsid:
            return {}

        result: dict = {
            "dtxsid": dtxsid,
            "@id": f"https://comptox.epa.gov/dashboard/chemical/details/{dtxsid}",
            "@type": "MolecularEntity",
            "name": hit.get("preferredName") or hit.get("name") or query,
        }
        if hit.get("casrn"):
            result["casrn"] = hit["casrn"]
        if hit.get("inchikey") or hit.get("inchiKey"):
            result["inchikey"] = hit.get("inchikey") or hit.get("inchiKey")
        return result
    except TransientLookupError:
        raise
    except Exception:
        return {}
