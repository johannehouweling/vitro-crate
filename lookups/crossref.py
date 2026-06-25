"""
Crossref REST API lookup.

Returns enriched citation metadata for a given DOI.
"""

from __future__ import annotations

import functools
import time
from urllib.parse import quote

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
            doi = doi[len(prefix) :]
            break

    doi_url = f"https://doi.org/{doi}"
    try:
        time.sleep(0.1)
        # A DOI's "/" is structural (e.g. "10.1016/j.tox..."), so keep slashes
        # but percent-encode spaces, "#", "?", "&", etc. so a malformed value
        # can't inject extra query params into the Crossref request (Issue #170).
        data = http_get_json(f"{_BASE}/{quote(doi, safe='/')}", headers=_HEADERS)
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


@functools.lru_cache(maxsize=256)
def search_works_by_title(title: str, rows: int = 5) -> tuple[dict, ...]:
    """Search Crossref for works whose bibliographic metadata matches *title*.

    A conservative title -> DOI search using Crossref's
    ``query.bibliographic`` field, ranked by Crossref's own relevance
    ``score`` (descending). This is the inverse of :func:`lookup_doi` (DOI ->
    metadata): given only a title, it returns the candidate works so a caller
    can apply a confidence gate before committing to a DOI (D5 — never fabricate
    an identifier).

    Args:
        title: Publication title to search for.
        rows: Maximum number of candidate works to return (Crossref ``rows``).

    Returns:
        A tuple of candidate dicts, each ``{"title", "doi", "score"}`` (the bare
        DOI, e.g. ``"10.1016/j.tox.2021.152898"``), ordered by descending
        ``score``. Empty when the title is blank or Crossref returns nothing.
        Raises :class:`TransientLookupError` on a transient API failure.

    Note:
        Returns a ``tuple`` (not a ``list``) so the ``lru_cache`` return value is
        immutable and cannot be mutated by a caller across cached calls.
    """
    query = title.strip()
    if not query:
        return ()
    try:
        time.sleep(0.1)
        data = http_get_json(
            _BASE,
            params={
                "query.bibliographic": query,
                "rows": str(max(1, int(rows))),
                # Ask only for the fields we score on, to keep the payload small.
                "select": "DOI,title,score",
            },
            headers=_HEADERS,
        )
        if data is NOT_FOUND:
            return ()

        items = data.get("message", {}).get("items", []) or []
        candidates: list[dict] = []
        for item in items:
            doi = str(item.get("DOI") or "").strip()
            if not doi:
                continue
            titles = item.get("title") or []
            name = titles[0] if titles else ""
            try:
                score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            candidates.append({"title": name, "doi": doi, "score": score})

        candidates.sort(key=lambda c: c["score"], reverse=True)
        return tuple(candidates)
    except TransientLookupError:
        raise
    except Exception:
        return ()
