"""
Cellosaurus REST API lookup.

Returns enriched, ontology-grounded cell-line metadata for a Cellosaurus
accession. Cellosaurus already cross-references each entry to standard
ontologies — species to NCBITaxon, disease to NCIt/ORDO, derived-from-site to
UBERON, plus a CLO (Cell Line Ontology) cross-reference — so this lookup pulls
those resolvable IRIs rather than discarding them: the values come back as
``schema:DefinedTerm`` objects carrying the obo PURL.
"""

from __future__ import annotations

import functools
from urllib.parse import quote

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://api.cellosaurus.org/cell-line"
_SEARCH_BASE = "https://api.cellosaurus.org/search/cell-line"

# Cross-reference databases worth surfacing as schema:sameAs (those that carry a
# resolvable IRI: CLO/BTO are OBO ontologies; Wikidata is a global hub).
_SAMEAS_DBS = {"CLO", "BTO", "Wikidata", "Cell_Model_Passport", "DepMap"}

# Name search: the Solr name fields queried separately, ``id`` first so a
# primary-identifier hit wins the dedup over the same entry found by synonym.
# See :func:`search_cellosaurus` for why the combined ``idsy`` field and a single
# boolean query both fail, and why the row count per field has to be this wide.
_SEARCH_FIELDS = ("id", "sy")
_SEARCH_ROWS_PER_FIELD = 50


def _term(d: dict) -> dict | None:
    """A Cellosaurus xref/list entry -> a schema:DefinedTerm (only if it has an IRI)."""
    iri = (d or {}).get("iri")
    return {"@id": iri, "@type": "DefinedTerm", "name": d.get("label", "")} if iri else None


@functools.lru_cache(maxsize=256)
def lookup_cellosaurus(accession: str) -> dict:
    """Return enriched cell-line properties for the given Cellosaurus accession.

    Args:
        accession: Cellosaurus accession, e.g. "CVCL_0631"

    Returns:
        dict suitable for spreading onto a cell-line Sample. Keys may include:
        name, alternateName, url, identifier, taxonomicRange (NCBITaxon
        DefinedTerm), disease (list of NCIt/ORDO DefinedTerms), anatomicalSite
        (UBERON DefinedTerm), donorSex, donorAge, category, sameAs (CLO/… IRIs).
        Returns {} on failure so callers can spread safely.
    """
    try:
        # Percent-encode the caller-supplied accession so a value containing a
        # "/" or ".." cannot break out of the cell-line path or inject query
        # params (Issue #170). ``safe=""`` encodes every reserved char.
        data = http_get_json(f"{_BASE}/{quote(accession, safe='')}?format=json")
        if data is NOT_FOUND:
            return {}
        # API wraps result: {"Cellosaurus": {"cell-line-list": [...]}}
        if "Cellosaurus" in data:
            cell_lines = data["Cellosaurus"].get("cell-line-list", [])
            data = cell_lines[0] if cell_lines else {}

        url = f"https://www.cellosaurus.org/{accession}"

        # name-list is a list of {"type": "identifier"/"synonym", "value": "..."}
        names: list = data.get("name-list", [])
        if isinstance(names, dict):
            names = names.get("name", [])
        primary = next((n for n in names if n.get("type") == "identifier"), None)
        name = primary.get("value", accession) if primary else accession
        alternate_names = [
            n.get("value", "") for n in names if n.get("type") == "synonym" and n.get("value")
        ]

        result: dict = {"name": name, "identifier": url, "url": url}
        if alternate_names:
            result["alternateName"] = alternate_names

        # species -> taxonomicRange (NCBITaxon DefinedTerm; label fallback)
        species_list = data.get("species-list", [])
        if isinstance(species_list, dict):
            species_list = [species_list]
        if species_list:
            result["taxonomicRange"] = _term(species_list[0]) or species_list[0].get("label", "")

        # disease(s) -> NCIt / ORDO DefinedTerms
        disease_list = data.get("disease-list", [])
        if isinstance(disease_list, dict):
            disease_list = [disease_list]
        diseases = [t for t in (_term(d) for d in disease_list) if t]
        if diseases:
            result["disease"] = diseases

        # derived-from-site -> UBERON DefinedTerm (anatomical origin)
        sites = data.get("derived-from-site-list", [])
        if isinstance(sites, dict):
            sites = [sites]
        for s in sites:
            site = (s or {}).get("site", {}) or {}
            term = next((_term(x) for x in (site.get("xref-list") or []) if x.get("iri")), None)
            if term:
                result["anatomicalSite"] = term
                break
            if site.get("value"):
                result.setdefault("anatomicalSite", site["value"])

        # donor / line facts (plain values; Cellosaurus does not ontologize these)
        if data.get("sex"):
            result["donorSex"] = data["sex"]
        if data.get("age"):
            result["donorAge"] = data["age"]
        if data.get("category"):
            result["category"] = data["category"]

        # cross-references -> sameAs (CLO and other resolvable hubs)
        same_as: list[str] = []
        for x in data.get("xref-list", []) or []:
            iri = x.get("iri")
            if x.get("database") in _SAMEAS_DBS and iri and iri not in same_as:
                same_as.append(iri)
        if same_as:
            result["sameAs"] = same_as[:8]

        return result

    except TransientLookupError:
        raise
    except Exception:
        return {}


def _candidate(cell_line: dict) -> dict | None:
    """A Cellosaurus search hit → ``{accession, name, synonyms}`` (or None).

    Returns None when the hit carries no primary accession or no identifier
    name, so a caller never sees a half-formed candidate.
    """
    accessions = cell_line.get("accession-list") or []
    if isinstance(accessions, dict):
        accessions = [accessions]
    accession = next(
        (a.get("value") for a in accessions if a.get("type") == "primary" and a.get("value")),
        None,
    )
    if not accession:
        # Fall back to the first accession with any value (defensive).
        accession = next((a.get("value") for a in accessions if a.get("value")), None)
    if not accession:
        return None

    names = cell_line.get("name-list") or []
    if isinstance(names, dict):
        names = names.get("name", [])
    primary = next((n for n in names if n.get("type") == "identifier" and n.get("value")), None)
    name = primary.get("value") if primary else None
    if not name:
        return None
    synonyms = [
        n.get("value", "") for n in names if n.get("type") == "synonym" and n.get("value")
    ]
    return {"accession": accession, "name": name, "synonyms": synonyms}


def _search_field(field: str, query: str, rows: int) -> list[dict]:
    """One Solr request against a single name field → parsed candidates.

    Args:
        field: Solr name field, ``"id"`` (primary identifier) or ``"sy"``
            (synonyms).
        query: Already-stripped cell-line name.
        rows: Maximum hits to request from this field.

    Returns:
        Parsed ``{accession, name, synonyms}`` candidates in the server's
        ranking order; ``[]`` on a definitive not-found or an unparseable body.

    Raises:
        TransientLookupError: on a transient API failure, so the caller can
            refuse to gate on a half-fetched candidate list.
    """
    try:
        # Quote the value so a name containing a space or reserved char cannot
        # break the query syntax.
        data = http_get_json(
            _SEARCH_BASE,
            params={
                "q": f'{field}:"{query}"',
                "fields": "id,ac,sy",
                "format": "json",
                "rows": str(max(1, int(rows))),
            },
        )
        if data is NOT_FOUND:
            return []

        cell_lines = (data or {}).get("Cellosaurus", {}).get("cell-line-list", [])
        if isinstance(cell_lines, dict):
            cell_lines = [cell_lines]
        return [c for c in (_candidate(cl) for cl in cell_lines) if c]
    except TransientLookupError:
        raise
    except Exception:
        return []


@functools.lru_cache(maxsize=256)
def search_cellosaurus(name: str, rows: int = _SEARCH_ROWS_PER_FIELD) -> tuple[dict, ...]:
    """Search Cellosaurus for cell lines whose name/synonym matches ``name``.

    A name → accession search using the Cellosaurus ``/search/cell-line``
    endpoint (Solr query). This is the inverse of :func:`lookup_cellosaurus`
    (accession → metadata): given only a cell-line *name* (e.g. ``"HepG2"``) it
    returns the candidate cell lines so a caller can apply a confidence gate
    before committing to an accession (D5 — never fabricate a ``CVCL_*`` id).

    The Solr fields match on tokens, so results include prefix/token matches
    (e.g. ``"HepG2 hALR"`` for ``"HepG2"``); the caller is responsible for
    exact-match disambiguation.

    **One request per name field, unioned (#385).** Cellosaurus is dominated by
    engineered derivatives whose *primary identifier* contains the parent's name
    as a token, and relevance ranking puts them above the parent: on the combined
    ``idsy`` field, ``CHO-K1`` (CVCL_0214) ranks 488 of 1116 and ``HepG2``
    (CVCL_0027) 53 of 88, so neither parent entered a 10-row candidate list at
    all. Querying ``id`` and ``sy`` separately puts the parent at or near the top
    of its field. Folding the two requests back into one does **not** work, and
    the alternatives were measured rather than assumed: at 50 rows,
    ``id:"X" OR sy:"X"`` leaves both parents outside the window, and boosting the
    identifier clause (``id:"X"^10 OR sy:"X"``) rescues CHO-K1 but still not
    HepG2. The endpoint's Solr syntax offers no exact-match operator, and ``sort``
    cannot express exactness either.

    Args:
        name: Cell-line name to search for (e.g. ``"HepG2"``, ``"A549"``).
        rows: Maximum hits to request **per name field**, so the union may return
            up to ``2 * rows`` candidates. The default of
            ``_SEARCH_ROWS_PER_FIELD`` is load-bearing, not cosmetic: ``HepG2``
            is only a *synonym* of CVCL_0027 (whose identifier is ``Hep-G2``) and
            ranks 21st of 45 on ``sy:"HepG2"``, so the union still misses it at
            10 rows per field.

    Returns:
        A tuple of candidate dicts, each ``{accession, name, synonyms}`` where
        ``accession`` is the bare Cellosaurus accession (e.g. ``"CVCL_0027"``),
        ``name`` is the primary identifier, and ``synonyms`` is a list of
        alternate names. Deduped by accession — an entry matched by both fields
        appears once, keeping the ``id`` hit — because a duplicate would read to
        the caller's gate as two exact matches and turn a resolvable name into an
        ambiguous one. Empty when the name is blank or Cellosaurus returns
        nothing.

    Raises:
        TransientLookupError: if *either* request fails transiently (timeout /
            connection / 429 / 5xx). A partial union is never returned: dropping
            one field's hits could silently turn an ambiguous name into a
            confident single match, which is a D5 violation.

    Note:
        Returns a ``tuple`` (not a ``list``) so the ``lru_cache`` return value is
        immutable and cannot be mutated by a caller across cached calls. Only
        this function is cached — caching ``_search_field`` as well would leave
        state behind ``search_cellosaurus.cache_clear()``.
    """
    query = name.strip()
    if not query:
        return ()
    candidates: list[dict] = []
    seen: set[str] = set()
    for field in _SEARCH_FIELDS:
        for candidate in _search_field(field, query, rows):
            accession = candidate["accession"]
            if accession in seen:
                continue
            seen.add(accession)
            candidates.append(candidate)
    return tuple(candidates)
