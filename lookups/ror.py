"""
ROR (Research Organization Registry) API lookup.

Searches for an organization by name and returns enriched metadata.
"""

from __future__ import annotations

import functools
import re
import unicodedata

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
    out = {
        "@id": ror_id,
        "@type": "Organization",
        "name": org_name,
        "url": url,
        "identifier": ror_id,
    }
    # The country of the first location ROR lists: its ISO 3166-1 alpha-2 code
    # and the name ROR prints for it.
    place = (record.get("locations") or [{}])[0].get("geonames_details") or {}
    if place.get("country_code"):
        out["addressCountry"] = place["country_code"]
        out["countryName"] = place.get("country_name") or place["country_code"]
    return out


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
        dict with keys: @id (ROR URL), @type, name, url, and, when ROR lists a
        location, addressCountry (the ISO country code) and countryName.
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


# Connectives that differ between an organization's registered name and the way it
# is written elsewhere, and carry no identifying weight either way.
_NAME_NOISE = frozenset({"of", "the", "and", "at", "for", "de", "der", "van", "di", "du"})


def _words(name: str) -> set[str]:
    """The words of *name* that identify an organization: without case, accents or connectives."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return {w for w in re.split(r"[^\w]+", folded.casefold()) if w and w not in _NAME_NOISE}


def same_name(registered: str, written: str) -> bool:
    """Whether a name ROR registers and a name written elsewhere are the same name.

    Checked BOTH ways, because each direction catches a different mistake.

    ROR's registered name rarely matches the written one character for character —
    it registers "Brunel University of London" where a paper prints "Brunel
    University London", and "Charité – Universitätsmedizin Berlin" where an ORCID
    record has "Charite-Universitätsmedizin Berlin" — so requiring containment
    rejects correct matches over a connective, an accent or a dash. Requiring only
    that ROR's words appear in the written name accepts a wrong one: "University of
    London" is a real and different institution whose words all appear inside
    "Brunel University London".

    Demanding the significant words agree in both directions keeps the first and
    refuses the second, because "Brunel" is missing from one side.
    """
    left, right = _words(registered), _words(written)
    return bool(left) and bool(right) and left == right


def _registered_names(record: dict) -> list[str]:
    """The names ROR registers for one organization: the one it displays, its labels and aliases.

    Not its acronyms: "VU" stands for several institutions, so an acronym names none
    of them. The second API generation lists every name in ``names[]`` with its
    types; the first gave ``name``, ``labels`` and ``aliases`` apart.
    """
    names = [
        n.get("value") or ""
        for n in record.get("names") or []
        if isinstance(n, dict) and "acronym" not in (n.get("types") or [])
    ]
    names.append(record.get("name") or "")
    labels = record.get("labels") or []
    names += [label.get("label") or "" for label in labels if isinstance(label, dict)]
    names += [alias for alias in record.get("aliases") or [] if isinstance(alias, str)]
    return [n for n in names if n]


@functools.lru_cache(maxsize=256)
def find_registered(name: str) -> dict:
    """The organization ROR registers under *name*, or ``{}``.

    Unlike :func:`search_ror`, which takes the search's first result, this answers
    only when the name IS one of the names a record registers (:func:`same_name`)
    and exactly one record among the results does: that is the organization, not a
    guess. A name no record registers, or several do, is no answer. The record
    comes in the shape :func:`search_ror` gives, with the name ROR displays.

    Raises TransientLookupError on a transient API failure (timeout / connection /
    429 / 5xx), so a caller can tell "ROR could not be asked" from "no record".
    """
    name = (name or "").strip()
    if not name:
        return {}
    try:
        data = http_get_json(_BASE, params={"query": name})
        if data is NOT_FOUND:
            return {}
        matches = {
            record.get("id"): record
            for record in data.get("items") or []
            if any(same_name(registered, name) for registered in _registered_names(record))
        }
        if len(matches) != 1:
            return {}
        return _extract(next(iter(matches.values())), fallback_name=name)
    except TransientLookupError:
        raise
    except Exception:
        return {}
