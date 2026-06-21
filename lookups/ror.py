"""
ROR (Research Organization Registry) API lookup.

Searches for an organization by name and returns enriched metadata.
"""

from __future__ import annotations

import functools
import time

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://api.ror.org/organizations"


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
        time.sleep(0.1)
        data = http_get_json(_BASE, params={"query": name})
        if data is NOT_FOUND:
            return {}

        items = data.get("items", [])
        if not items:
            return {}

        best = items[0]
        ror_id = best.get("id", "")          # e.g. "https://ror.org/02jz4aj89"
        # ROR v2: name may be under names list; v1: direct "name" field
        org_name = best.get("name") or name
        if not org_name:
            names_list = best.get("names", [])
            if names_list:
                org_name = names_list[0].get("value", name)
        # links is a list of {"type": ..., "value": url}
        links = best.get("links", [])
        website = next((lk["value"] for lk in links if isinstance(lk, dict) and lk.get("type") == "website"), "")
        url = website or (links[0] if links and isinstance(links[0], str) else "")

        return {
            "@id": ror_id,
            "@type": "Organization",
            "name": org_name,
            "url": url,
            "identifier": ror_id,
        }
    except TransientLookupError:
        raise
    except Exception:
        return {}
