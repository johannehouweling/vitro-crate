"""
Crossref REST API lookup.

Returns enriched citation metadata for a given DOI.
"""

from __future__ import annotations

import functools
import time

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://api.crossref.org/works"
_HEADERS = {"User-Agent": "rocrate-wizard/0.1 (mailto:support@example.com)"}


@functools.lru_cache(maxsize=256)
def lookup_doi(doi: str) -> dict:
    """Return citation metadata for a DOI from Crossref.

    Args:
        doi: DOI string, e.g. "10.1016/j.tox.2021.152898"
              (with or without the "https://doi.org/" prefix)

    Returns:
        dict with keys: @id, @type ("ScholarlyArticle"), name, author (list),
        datePublished, url, identifier.
        Returns {} when the DOI is not found. Raises TransientLookupError on a
        transient API failure (timeout / connection / 429 / 5xx).
    """
    doi = doi.strip()
    # Strip URL prefix if present
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "DOI:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break

    doi_url = f"https://doi.org/{doi}"
    try:
        time.sleep(0.1)
        data = http_get_json(f"{_BASE}/{doi}", headers=_HEADERS)
        if data is NOT_FOUND:
            return {}

        work = data.get("message", {})

        # Title
        titles = work.get("title", [])
        name = titles[0] if titles else ""

        # Authors
        authors = []
        for a in work.get("author", []):
            entry: dict = {}
            if a.get("given"):
                entry["givenName"] = a["given"]
            if a.get("family"):
                entry["familyName"] = a["family"]
            if a.get("ORCID"):
                entry["identifier"] = a["ORCID"]
            if entry:
                authors.append(entry)

        # Publication year
        issued = work.get("issued", {}).get("date-parts", [[]])
        year = str(issued[0][0]) if issued and issued[0] else ""

        # Publisher URL
        url = work.get("URL", doi_url)

        return {
            "@id": doi_url,
            "@type": "ScholarlyArticle",
            "identifier": doi_url,
            "name": name,
            # ISA RO-Crate profile requires schema:headline on a ScholarlyArticle;
            # mirror the title so consumers get it for free (schema:name is kept
            # too for schema.org / Bioschemas / human-preview consumers).
            "headline": name,
            "author": authors,
            "datePublished": year,
            "url": url,
        }
    except TransientLookupError:
        raise
    except Exception:
        return {}
