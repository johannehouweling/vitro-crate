"""Recover an author's affiliation from the papers their ORCID record lists.

Many ORCID records carry no employment at all — the researcher never filled that
section in — and the profile still asks every author for an institution. The
papers are usually right there on the same record, and Crossref names the
affiliation on the author line, so the fact exists; it is simply one hop away:

    ORCID iD -> works on that record -> DOIs -> Crossref -> affiliation string

This is retrieval, not inference. Every value returned was published by a
registry about this person; nothing is guessed from a name, an email domain, or
a co-author.

WHEN TO TRUST IT (the rule this module implements):

* every affiliation found agrees -> take it, even from two papers. Two
  independent records saying the same institution is corroboration, and
  demanding a third would throw away a good answer for someone who has only
  published twice.
* they disagree -> the THREE MOST RECENT decide. People move; an old paper is
  evidence of where someone used to be, which is a different claim.
* still split -> return nothing and let a human answer. A confident wrong
  affiliation in a scientific record is worse than an open question.

Author matching prefers the ORCID Crossref itself carries; a family-name match
is the fallback. Names alone would pick the wrong person on a paper with two
Schmidts, and the affiliation would look perfectly plausible.
"""

from __future__ import annotations

import functools
import re
from collections import Counter
from urllib.parse import quote

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_ORCID_BASE = "https://pub.orcid.org/v3.0"
_CROSSREF_BASE = "https://api.crossref.org/works"
_ORCID_HEADERS = {"Accept": "application/json"}

# How many of the record's works to dereference. Enough for the agreement rule
# to have something to weigh, bounded so one prolific author cannot turn a build
# into a hundred HTTP round-trips.
_MAX_WORKS = 8

# How many recent works break a tie, per the rule above.
_RECENT_WINDOW = 3


def _bare(orcid_id: str) -> str:
    return (orcid_id or "").strip().rstrip("/").rsplit("/", 1)[-1]


def _publication_year(summary: dict) -> int:
    """The work's year, or 0 when ORCID does not state one (sorts oldest)."""
    date = (summary.get("publication-date") or {}) or {}
    year = ((date.get("year") or {}) or {}).get("value")
    try:
        return int(str(year))
    except (TypeError, ValueError):
        return 0


def recent_dois(orcid_id: str, limit: int = _MAX_WORKS) -> list[str]:
    """DOIs from an ORCID record's works, most recently published first.

    Returns ``[]`` when the record has no works, no DOIs, or does not resolve.
    Raises TransientLookupError so a caller can distinguish an outage from an
    author who genuinely has none.
    """
    bare = _bare(orcid_id)
    if not bare:
        return []
    data = http_get_json(f"{_ORCID_BASE}/{quote(bare, safe='')}/works", headers=_ORCID_HEADERS)
    if data is NOT_FOUND or not isinstance(data, dict):
        return []

    dated: list[tuple[int, str]] = []
    for group in data.get("group") or []:
        summaries = group.get("work-summary") or [{}]
        year = max((_publication_year(s) for s in summaries), default=0)
        for ext in (group.get("external-ids") or {}).get("external-id") or []:
            if str(ext.get("external-id-type") or "").lower() == "doi":
                value = str(ext.get("external-id-value") or "").strip()
                if value:
                    dated.append((year, value))
                    break
    dated.sort(key=lambda pair: -pair[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, doi in dated:
        key = doi.casefold()
        if key not in seen:
            seen.add(key)
            out.append(doi)
        if len(out) >= limit:
            break
    return out


def _author_matches(author: dict, orcid: str, family: str) -> bool:
    """Whether a Crossref author line is the person we are asking about.

    The ORCID Crossref carries is decisive when present. Falling back to the
    family name is deliberate but weaker, and is why the caller still requires
    agreement across papers before believing the answer.
    """
    listed = _bare(str(author.get("ORCID") or ""))
    if listed and orcid:
        return listed.casefold() == orcid.casefold()
    if not family:
        return False
    return str(author.get("family") or "").strip().casefold() == family.strip().casefold()


def _affiliations_on(doi: str, orcid: str, family: str) -> list[str]:
    """Affiliation strings Crossref lists for this person on this DOI."""
    try:
        data = http_get_json(f"{_CROSSREF_BASE}/{quote(doi, safe='/')}")
    except TransientLookupError:
        raise
    except Exception:
        return []
    if data is NOT_FOUND or not isinstance(data, dict):
        return []
    message = data.get("message") or {}
    out: list[str] = []
    for author in message.get("author") or []:
        if not _author_matches(author, orcid, family):
            continue
        for affiliation in author.get("affiliation") or []:
            name = str((affiliation or {}).get("name") or "").strip()
            if name:
                out.append(name)
    return out


# Punctuation and postal noise that stop two spellings of one institution from
# matching. Crossref affiliation strings are free text: the same university
# arrives as "Brunel University London, Uxbridge, UK." and "…, Kingston Lane,
# Uxbridge UB8 3PH, U.K.".
_NOISE = re.compile(r"[.,;]|\b[A-Z]{1,2}\d{1,2}\s*\d?[A-Z]{0,2}\b")


def institution_of(affiliation: str) -> str:
    """The employer named inside a Crossref affiliation string.

    Crossref publishes affiliations as free text with the department, street and
    postcode attached — "Centre for Pollution Research and Policy, Brunel
    University London, Kingston Lane, Uxbridge UB8 3PH, U.K." Storing that as an
    Organization name is not wrong so much as useless: it will never match
    another spelling of the same employer, and it is not what anyone would call
    the institution. This returns the part that is ("Brunel University London"),
    ready to look up in ROR.
    """
    parts = [p.strip() for p in (affiliation or "").split(",") if p.strip()]
    best_rank, chosen = len(_INSTITUTION_WORDS), (affiliation or "").strip()
    for part in parts:
        lowered = part.casefold()
        for rank, word in enumerate(_INSTITUTION_WORDS):
            if word in lowered and rank < best_rank:
                best_rank, chosen = rank, part
                break
    return chosen


def _institution_key(affiliation: str) -> str:
    """A comparison key for one affiliation string.

    Departments, streets and postcodes differ between records of the same
    employer, so the key keeps the longest comma-separated part that looks like
    an institution — the segment naming a university, institute, hospital or
    centre — and falls back to the whole string when none does.
    """
    # Strength beats length: one record spells the employer "Centre for
    # Pollution Research and Policy, Brunel University London, …" and another
    # "Institute of …, Brunel University London, …". Taking the LONGEST
    # institution-ish part picks the department both times, and two spellings of
    # one employer then look like two employers. The university outranks
    # whatever sits in front of it.
    return " ".join(_NOISE.sub(" ", institution_of(affiliation)).split()).casefold()


# Institution words, STRONGEST FIRST. A department, centre or laboratory is part
# of an employer, not the employer, so it only names the affiliation when
# nothing broader is present in the string.
_INSTITUTION_WORDS: tuple[str, ...] = (
    "universit",
    "hochschule",
    "polytechnic",
    "hospital",
    "college",
    "academy",
    "institut",
    "school",
    "centre",
    "center",
    "laborator",
    "museum",
    "agency",
    "council",
)


def affiliation_from_works(
    orcid_id: str,
    family_name: str = "",
    *,
    limit: int = _MAX_WORKS,
) -> str:
    """The institution this person's papers agree on, or ``""``.

    Implements the trust rule in the module docstring: unanimous wins outright,
    a disagreement is settled by the three most recent works, and anything still
    split returns ``""`` so the caller asks a human instead of guessing.

    Args:
        orcid_id: The author's ORCID (bare or as a URL).
        family_name: Used to match the author on papers where Crossref carries
            no ORCID. Without it only ORCID-tagged author lines are read.
        limit: How many works to dereference at most.

    Returns:
        The affiliation string as Crossref published it — the caller resolves it
        to an Organization (ROR) rather than storing prose. ``""`` when the
        record has no works, none names an affiliation, or the evidence
        conflicts and recency cannot settle it.
    """
    bare = _bare(orcid_id)
    if not bare:
        return ""
    try:
        dois = recent_dois(bare, limit=limit)
    except TransientLookupError:
        raise
    except Exception:
        return ""

    # Most recent first, so an ordered list is already the recency ranking.
    found: list[str] = []
    for doi in dois:
        found.extend(_affiliations_on(doi, bare, family_name))
    if not found:
        return ""

    keys = [_institution_key(a) for a in found]
    if len(set(keys)) == 1:
        return found[0]

    recent = keys[:_RECENT_WINDOW]
    winner, count = Counter(recent).most_common(1)[0]
    # A plurality is not agreement: with three sources split 2/1 the majority
    # decides, but a 1/1/1 split has no answer and must go to a human.
    if count <= len(recent) / 2:
        return ""
    return next(a for a, k in zip(found, keys, strict=True) if k == winner)


@functools.lru_cache(maxsize=256)
def cached_affiliation_from_works(orcid_id: str, family_name: str = "") -> str:
    """Cached :func:`affiliation_from_works` — several authors share a paper."""
    return affiliation_from_works(orcid_id, family_name)
