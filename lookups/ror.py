"""
ROR (Research Organization Registry) API lookup.

Searches for an organization by name and returns enriched metadata.
"""

from __future__ import annotations

import functools

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://api.ror.org/organizations"


def _extract(record: dict, fallback_name: str = "") -> dict:
    """Map one ROR record to our Organization shape.

    Shared by the search and by-id entry points because ROR serves the same
    record from both, and the two API generations differ in the same two places:
    the display name (v1 ``name``, v2 ``names[]`` with a ``ror_display`` type)
    and ``links`` (v1 plain strings, v2 ``{type, value}`` objects). Handling both
    here keeps the difference in one place.
    """
    ror_id = record.get("id", "")
    org_name = record.get("name") or ""
    if not org_name:
        names = record.get("names", [])
        display = next(
            (n for n in names if isinstance(n, dict) and "ror_display" in (n.get("types") or [])),
            None,
        )
        org_name = (display or (names[0] if names else {}) or {}).get("value", "") or fallback_name
    links = record.get("links", [])
    website = next(
        (lk["value"] for lk in links if isinstance(lk, dict) and lk.get("type") == "website"),
        "",
    )
    url = website or (links[0] if links and isinstance(links[0], str) else "")
    return {
        "@id": ror_id,
        "@type": "Organization",
        "name": org_name,
        "url": url,
        "identifier": ror_id,
    }


@functools.lru_cache(maxsize=256)
def fetch_ror_by_id(ror_id: str) -> dict:
    """Fetch one organization by its ROR IRI or bare id — an exact lookup.

    Distinct from :func:`search_ror`, which guesses from a name string. Here the
    identifier is already known (ORCID states it on an employment record, or a
    human supplied it), so the record returned is the right one by construction
    and its ``url`` can be trusted without a human confirming the match.

    Args:
        ror_id: "https://ror.org/04pp8hn57" or "04pp8hn57".

    Returns:
        Same shape as :func:`search_ror`; ``{}`` when the id does not resolve.
        Raises TransientLookupError on a transient API failure.
    """
    bare = (ror_id or "").strip().rsplit("/", 1)[-1]
    if not bare:
        return {}
    try:
        data = http_get_json(f"{_BASE}/{bare}")
        if data is NOT_FOUND or not isinstance(data, dict) or not data.get("id"):
            return {}
        return _extract(data)
    except TransientLookupError:
        raise
    except Exception:
        return {}


@functools.lru_cache(maxsize=256)
def search_ror(name: str) -> dict:
    """Search ROR for an organization by name; return the top match.

    Args:
        name: organization name, e.g. "Maastricht University"

    Returns:
        dict with keys: @id (ROR URL), @type, name, url.
        Returns {} on no match. Raises TransientLookupError on a transient API
        failure (timeout / connection / 429 / 5xx).
    """
    if not name:
        return {}
    try:
        data = http_get_json(_BASE, params={"query": name})
        if data is NOT_FOUND:
            return {}

        items = data.get("items", [])
        if not items:
            return {}

        return _extract(items[0], fallback_name=name)
    except TransientLookupError:
        raise
    except Exception:
        return {}
