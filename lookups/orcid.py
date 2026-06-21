"""
ORCID public API lookup.

Returns enriched Person metadata for a given ORCID iD.
No authentication required (uses the public ORCID API).
"""

from __future__ import annotations

import functools
import time

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://pub.orcid.org/v3.0"
_HEADERS = {"Accept": "application/json"}


@functools.lru_cache(maxsize=256)
def lookup_orcid(orcid_id: str) -> dict:
    """Return enriched Person properties for the given ORCID iD.

    Args:
        orcid_id: bare ORCID iD, e.g. "0000-0001-6004-8653"

    Returns:
        dict with keys: @id, @type, identifier, name, givenName, familyName,
        affiliation_name (str), affiliation_ror (str, may be ""). The caller
        is responsible for creating an Organization entity from these fields.
        Returns {} when the iD is not found. Raises TransientLookupError on a
        transient API failure (timeout / connection / 429 / 5xx) so a momentary
        outage is never mistaken for a resolved (or unresolvable) record.
    """
    orcid_url = f"https://orcid.org/{orcid_id}"
    try:
        time.sleep(0.1)
        data = http_get_json(f"{_BASE}/{orcid_id}/record", headers=_HEADERS)
        if data is NOT_FOUND:
            return {}

        person = data.get("person", {})
        name_block = person.get("name") or {}
        given = (name_block.get("given-names") or {}).get("value", "")
        family = (name_block.get("family-name") or {}).get("value", "")
        full_name = f"{given} {family}".strip()

        # First employment affiliation
        activities = data.get("activities-summary") or {}
        employments = activities.get("employments") or {}
        groups = employments.get("affiliation-group") or []
        affiliation_name = ""
        affiliation_ror = ""
        if groups:
            summaries = (groups[0].get("summaries") or [{}])
            emp = (summaries[0].get("employment-summary") or {})
            org = emp.get("organization") or {}
            affiliation_name = org.get("name", "")
            disambig = org.get("disambiguated-organization") or {}
            if (disambig.get("disambiguation-source") or "").upper() == "ROR":
                ror_value = disambig.get("disambiguated-organization-identifier", "")
                if ror_value:
                    affiliation_ror = (
                        ror_value if ror_value.startswith("http")
                        else f"https://ror.org/{ror_value}"
                    )

        return {
            "@id": orcid_url,
            "@type": "Person",
            "identifier": orcid_url,
            "givenName": given,
            "familyName": family,
            "name": full_name or orcid_id,
            "affiliation_name": affiliation_name,
            "affiliation_ror": affiliation_ror,
        }
    except TransientLookupError:
        raise
    except Exception:
        return {}
