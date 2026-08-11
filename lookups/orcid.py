"""
ORCID public API lookup.

Returns enriched Person metadata for a given ORCID iD.
No authentication required (uses the public ORCID API).
"""

from __future__ import annotations

import functools
import re
from urllib.parse import quote

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://pub.orcid.org/v3.0"
_HEADERS = {"Accept": "application/json"}

# 16 characters as four hyphenated groups; the final one may be "X" (check digit 10).
_ORCID_SHAPE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def is_well_formed_orcid(orcid_id: str) -> bool:
    """Whether *orcid_id* is a structurally valid ORCID iD — shape AND check digit.

    An ORCID iD carries an ISO 7064 MOD 11-2 check digit, so a mistyped or
    misread iD is detectable without asking ORCID about it. This is a pure
    function: it never touches the network.

    Deliberately NOT called by :func:`lookup_orcid`, which stays a thin transport
    wrapper — a caller holding a synthetic iD (tests, fixtures) must still be able
    to exercise the 404 and timeout paths. The agent-facing wrapper in
    ``builder.tools.lookups`` is where a malformed iD is worth catching, because
    that is the layer that has to tell the model what to do about it.

    Args:
        orcid_id: Bare ORCID iD, e.g. "0000-0001-6004-8653". A bare iD only —
            a full ``https://orcid.org/…`` URL is not accepted.

    Returns:
        True when the shape matches and the check digit agrees with the first 15
        digits; False otherwise.
    """
    if not _ORCID_SHAPE.match(orcid_id or ""):
        return False
    digits = orcid_id.replace("-", "")
    total = 0
    for char in digits[:15]:
        total = (total + int(char)) * 2
    expected = (12 - total % 11) % 11
    return ("X" if expected == 10 else str(expected)) == digits[15]


@functools.lru_cache(maxsize=256)
def lookup_orcid(orcid_id: str) -> dict:
    """Return enriched Person properties for the given ORCID iD.

    Args:
        orcid_id: bare ORCID iD, e.g. "0000-0001-6004-8653"

    Returns:
        dict with keys: @id, @type, identifier, name, givenName, familyName,
        affiliation_name (str), affiliation_ror (str, may be ""), job_title
        (str, may be ""). The caller
        is responsible for creating an Organization entity from these fields.
        Returns {} when the iD is not found. Raises TransientLookupError on a
        transient API failure (timeout / connection / 429 / 5xx) so a momentary
        outage is never mistaken for a resolved (or unresolvable) record.
    """
    orcid_url = f"https://orcid.org/{orcid_id}"
    try:
        # Percent-encode the caller-supplied iD for the request path so a "/" or
        # ".." cannot escape the /record path or inject query params (Issue #170).
        data = http_get_json(f"{_BASE}/{quote(orcid_id, safe='')}/record", headers=_HEADERS)
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
        # The same employment summary that names the organisation also names the
        # role. We were reading one and discarding the other, and the ISA profile
        # asks every Person for a job title — so the finding was answerable from
        # data already on the wire. Absent for plenty of researchers, and then it
        # stays absent: an empty role is not something to invent.
        job_title = ""
        if groups:
            summaries = groups[0].get("summaries") or [{}]
            emp = summaries[0].get("employment-summary") or {}
            org = emp.get("organization") or {}
            affiliation_name = org.get("name", "")
            job_title = (emp.get("role-title") or "").strip()
            disambig = org.get("disambiguated-organization") or {}
            if (disambig.get("disambiguation-source") or "").upper() == "ROR":
                ror_value = disambig.get("disambiguated-organization-identifier", "")
                if ror_value:
                    affiliation_ror = (
                        ror_value
                        if ror_value.startswith("http")
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
            "job_title": job_title,
        }
    except TransientLookupError:
        raise
    except Exception:
        return {}


@functools.lru_cache(maxsize=256)
def _search_orcid_by_name(
    given: str, family: str, affiliation: str | None
) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Cached ORCID expanded-search returning hashable candidate tuples.

    Returns a tuple of candidates, each a tuple of ``(key, value)`` pairs, so the
    cached value is immutable and never shared-mutated. :func:`lookup_orcid_by_name`
    rehydrates these into fresh dicts for callers.
    """
    family = (family or "").strip()
    given = (given or "").strip()
    if not family:
        return ()

    # Lucene query against ORCID's indexed name fields. Family name is the
    # strongest signal; given name and affiliation narrow it when present.
    terms = [f'family-name:"{family}"']
    if given:
        terms.append(f'given-names:"{given}"')
    if affiliation:
        terms.append(f'affiliation-org-name:"{affiliation.strip()}"')
    query = " AND ".join(terms)

    try:
        data = http_get_json(
            f"{_BASE}/expanded-search/",
            params={"q": query, "rows": "10"},
            headers=_HEADERS,
        )
        if data is NOT_FOUND:
            return ()

        out: list[tuple[tuple[str, str], ...]] = []
        for row in data.get("expanded-result") or []:
            orcid_id = row.get("orcid-id")
            if not orcid_id:
                continue
            institutions = row.get("institution-name") or []
            out.append(
                (
                    ("orcid", str(orcid_id)),
                    ("given", row.get("given-names") or ""),
                    ("family", row.get("family-names") or ""),
                    ("affiliation", institutions[0] if institutions else ""),
                )
            )
        return tuple(out)
    except TransientLookupError:
        raise
    except Exception:
        return ()


def lookup_orcid_by_name(given: str, family: str, affiliation: str | None = None) -> list[dict]:
    """Search the ORCID public registry for people matching a name.

    Uses the ORCID public ``/v3.0/expanded-search`` endpoint (no auth) via the
    shared rate-limited HTTP layer, so candidate ORCID iDs can be discovered for
    a citation author who is not already in the crate. The caller is responsible
    for disambiguating and verifying any candidate before use (D5: never attach
    an unverified ORCID).

    Args:
        given: The author's given (first) name. May be an initial.
        family: The author's family (last) name. Required — a blank family name
            returns no candidates without issuing a request.
        affiliation: Optional institution name; when given it is added to the
            query to bias the search, but candidates from any affiliation are
            still returned (ranking/filtering is left to the caller).

    Returns:
        A list of candidate dicts, each ``{orcid, given, family, affiliation}``
        in the order ORCID returned them. Empty when nothing matched. Raises
        :class:`TransientLookupError` on a transient API failure so a momentary
        outage is never mistaken for "no such person".
    """
    return [dict(candidate) for candidate in _search_orcid_by_name(given, family, affiliation)]


# Expose the underlying cache so tests can clear it like the other lookups.
lookup_orcid_by_name.cache_clear = _search_orcid_by_name.cache_clear  # ty: ignore[unresolved-attribute]
