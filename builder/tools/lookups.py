"""Unified lookup interface for the ISA-Tox RO-Crate Builder.

Wraps the existing lookup API clients (PubChem, Cellosaurus, AOP-Wiki,
BAO/OLS, ORCID, ROR, Crossref) into a consistent ``{found, data, error}``
interface. Each function is LRU-cached and rate-limited.

Multi-strategy lookups:
    - lookup_compound: tries name → CAS → PubChem CID
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable
from typing import Any

from builder.tools._resolve_cache import compound_cache, normalize_compound_name
from lookups._http import TransientLookupError
from lookups.aopwiki import lookup_aop as lookup_aop_wiki
from lookups.bao import lookup_bao_term as lookup_bao_term_ols
from lookups.bao import lookup_ontology_term as lookup_ontology_term_ols
from lookups.bao import lookup_unit as lookup_unit_ols
from lookups.cellosaurus import lookup_cellosaurus, search_cellosaurus
from lookups.comptox import lookup_dtxsid as lookup_dtxsid_comptox
from lookups.crossref import lookup_doi as lookup_doi_crossref
from lookups.orcid import is_well_formed_orcid
from lookups.orcid import lookup_orcid as lookup_orcid_api
from lookups.pubchem import lookup_pubchem
from lookups.ror import search_ror

logger = logging.getLogger(__name__)

# Standard return type for all lookup functions
LOOKUP_RESULT = dict[str, Any]  # {"found": bool, "data": dict, "error": str | None}


def _success(data: dict) -> dict[str, Any]:
    """Wrap successful lookup data into the standard result format."""
    return {"found": True, "data": data, "error": None}


def _failure(error: str, transient: bool = False, fix: str | None = None) -> dict[str, Any]:
    """Wrap a failure into the standard result format.

    ``transient=True`` marks a temporary outage (timeout / 429 / 5xx) as
    distinct from a definitive not-found, so callers (e.g. verify_identifier)
    can keep the user's value and retry rather than treat it as unresolved.

    ``fix`` states what to DO about a definitive failure, in the same spirit as
    the ``fix`` key on a validation issue. A bare "no" is what makes the model
    wander: told only that a lookup failed, it has no next action and re-runs
    the neighbouring lookups that DO succeed. One observed run re-issued the
    same successful ORCID lookup 8 times after an adjacent one came back
    unresolvable, burning ~42s of model turns on a cached answer it already
    held. Omitted when the failure is transient — there the next action is
    simply to retry, and the caller already knows that from ``transient``.
    """
    result = {"found": False, "data": {}, "error": error, "transient": transient}
    if fix:
        result["fix"] = fix
    return result


@functools.lru_cache(maxsize=256)
def lookup_compound(name: str) -> dict[str, Any]:
    """Look up a chemical compound by name via PubChem.

    Consults a shared in-process cache (keyed by *normalized* name) before doing
    any network work, so a repeated compound — or a re-resolution by a resolved
    CAS / ``CID <cid>`` alias key warmed by :func:`resolve_compound` — is served
    without a fresh PubChem round-trip (Issue #252). Only successful and
    definitive-not-found results are cached; a transient failure is never stored
    (mirroring the ``lru_cache`` "errors raise, so are not cached" contract), so a
    retry re-hits the network.

    Args:
        name: Compound name (e.g. "Silychristin A").

    Returns:
        Standard lookup dict with keys:
            found: True if compound was found.
            data: Dict with cas, smiles, inchikey, inchi, formula, mass,
                  iupac_name, pubchem_cid keys (or empty dict).
            error: Error message or None on success.
    """
    cache_key = normalize_compound_name(name)
    cached = compound_cache.get(cache_key) if cache_key else None
    if cached is not None:
        return cached

    result = _lookup_compound_uncached(name)
    # Cache successful and definitive not-found results (not transient outages),
    # so a later retry of a transient failure still re-hits the source.
    if cache_key and not result.get("transient"):
        compound_cache.put(cache_key, result)
    return result


def _lookup_compound_uncached(name: str) -> dict[str, Any]:
    """Resolve a compound from PubChem (then a ChEBI fallback) with no caching.

    The cache-aware :func:`lookup_compound` wraps this; splitting it out keeps the
    in-process cache concern out of the resolution logic.
    """
    try:
        # Multi-strategy: try as name first, then CAS/CID-style variants.
        raw = name.strip()
        candidates: list[str] = [raw]
        lowered = raw.lower()
        if lowered.startswith("cas "):
            candidates.append(raw[4:].strip())
        if lowered.startswith("cid "):
            candidates.append(raw[4:].strip())
        if lowered.startswith("pubchem:"):
            candidates.append(raw.split(":", 1)[1].strip())

        seen: set[str] = set()
        for q in candidates:
            if not q or q in seen:
                continue
            seen.add(q)
            result = lookup_pubchem(q)
            if result and result.get("pubchem_cid"):
                return _success(result)

        # Multi-strategy fallback: PubChem found nothing, so try ChEBI via OLS
        # (name → CAS → ChEBI, as documented in AGENTS.md §10). This resolves a
        # ChEBI ontology IRI for compounds PubChem does not index, rather than
        # returning a hard "not found".
        chebi = lookup_ontology_term_ols(raw, "chebi")
        if chebi and chebi.get("@id"):
            # Express ChEBI identity with context-declared keys so the
            # MolecularEntity compacts cleanly under RO-Crate 1.2 (Issue #243).
            # Bare ``chebi_id`` / ``chebi_iri`` keys are absent from the @context
            # and fail base-profile validation. ``chebiId`` (the ChEBI CURIE) is
            # already mapped to schema:identifier — mirroring ``cas`` / ``pubchemCid``
            # — and the dereferenceable ontology IRI rides on ``sameAs`` as an
            # ``@id`` node, the idiomatic machine-resolvable identity link.
            data: dict[str, Any] = {
                "iupac_name": chebi.get("name", raw),
                "source": "chebi",
                "sameAs": {"@id": chebi["@id"]},
            }
            term_code = chebi.get("termCode")
            if term_code:
                data["chebiId"] = term_code
            return _success(data)

        return _failure(f"Compound '{name}' not found in PubChem or ChEBI")
    except TransientLookupError as exc:
        return _failure(f"PubChem unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("PubChem lookup failed for '%s'", name)
        return _failure(f"PubChem lookup failed: {exc}")


def warm_compound_cache(name: str, result: dict[str, Any]) -> None:
    """Prime the in-process compound cache for ``name`` and its identifier aliases.

    After :func:`resolve_compound` resolves a compound once, the subsequent CAS /
    ``CID <cid>`` *verification* re-resolutions hit :func:`lookup_compound` with a
    different key (the bare CAS, ``"CID <cid>"``) than the original name — which
    would otherwise be cache misses and fire fresh PubChem round-trips. Warming
    those alias keys here points them at the SAME authoritative record, collapsing
    the verify step's two re-resolutions to zero network calls (Issue #252). D5 is
    preserved: the alias keys carry the exact record PubChem returned, so the
    verify still confirms identifiers against the authority's own answer.

    A transient or not-found result is not warmed (only a real hit has aliases to
    register), so a retry can still re-hit the source.
    """
    if not result.get("found"):
        return
    name_key = normalize_compound_name(name)
    if name_key:
        compound_cache.put(name_key, result)
    data = result.get("data") or {}
    cas = str(data.get("cas") or "").strip()
    if cas:
        compound_cache.put(normalize_compound_name(cas), result)
    cid = str(data.get("pubchem_cid") or "").strip()
    if cid:
        # verify_identifier re-resolves a CID as the query "CID <cid>".
        compound_cache.put(normalize_compound_name(f"CID {cid}"), result)


@functools.lru_cache(maxsize=256)
def lookup_cell_line(accession: str) -> dict[str, Any]:
    """Look up a cell line by its Cellosaurus accession.

    Args:
        accession: Cellosaurus accession, e.g. "CVCL_0631".

    Returns:
        Standard lookup dict with keys:
            found: True if cell line was found.
            data: Dict with name, identifier, url, and optionally
                  alternateName, taxonomicRange, disease, etc.
            error: Error message or None on success.
    """
    try:
        result = lookup_cellosaurus(accession)
        if result and result.get("name"):
            return _success(result)
        return _failure(f"Cell line '{accession}' not found in Cellosaurus")
    except TransientLookupError as exc:
        return _failure(f"Cellosaurus unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("Cellosaurus lookup failed for '%s'", accession)
        return _failure(f"Cellosaurus lookup failed: {exc}")


def _normalize_cell_line_name(value: str) -> str:
    """Normalize a cell-line name for exact-match comparison (strip + casefold)."""
    return " ".join((value or "").split()).casefold()


def cell_line_names_match(
    query: str, primary: str, synonyms: Iterable[str] | None = ()
) -> bool:
    """Return True if *query* names the Cellosaurus record ``primary``/``synonyms``.

    The single definition of "this name matches this record", shared by the two
    callers that must not drift apart (#383):

    - :func:`lookup_cell_line_by_name`, deciding whether a name-search candidate
      is a confident enough match to commit its accession, and
    - :func:`builder.tools.verification.verify_identifier`, deciding whether an
      accession that *resolves* actually resolves to the entity in hand.

    The comparison is deliberately exact (whitespace-collapsed, case-folded)
    against the record's primary identifier and every synonym. It is not fuzzy:
    an engineered derivative such as ``"CHO-K1 hOATP1C1"`` does NOT match its
    parent record ``"CHO-K1"``, and callers are expected to treat that as
    "unconfirmed", never as "wrong" — a near-miss here is not evidence the
    accession is bad, only that this function cannot vouch for it.
    """
    target = _normalize_cell_line_name(query)
    if not target:
        return False
    if _normalize_cell_line_name(primary) == target:
        return True
    return any(_normalize_cell_line_name(s) == target for s in synonyms or ())


@functools.lru_cache(maxsize=256)
def lookup_cell_line_by_name(name: str) -> dict[str, Any]:
    """Resolve a cell-line *name* to its Cellosaurus accession (confidence-gated).

    The accession-based :func:`lookup_cell_line` requires a ``CVCL_*`` id up
    front; this is the inverse — given a bare cell-line name (e.g. ``"HepG2"``,
    ``"A549"``) it searches Cellosaurus and, **only on a confident exact match**,
    returns the accession plus minimal metadata.

    **D5 confidence gate (Verify, Don't Trust).** A Solr name search returns
    prefix/token matches as well as exact ones, so committing the top hit would
    risk a fabricated accession. The accession is committed ONLY when exactly one
    candidate's primary identifier *or* a synonym equals the queried name
    (case-insensitive, whitespace-collapsed). Zero exact matches, or two-or-more
    (ambiguous), returns the standard ``{found: False}`` failure — an accession
    is never guessed.

    Args:
        name: Cell-line name to resolve (e.g. ``"HepG2"``).

    Returns:
        Standard lookup dict with keys:
            found: True only on a confident, unambiguous exact match.
            data: ``{accession, name, synonyms}`` on success, else ``{}``.
            error: Error message, or None on success.
        A transient outage is flagged ``transient=True`` so callers can retry
        rather than treat it as a definitive miss.
    """
    query = (name or "").strip()
    if not query:
        return _failure("No cell-line name provided")
    try:
        candidates = search_cellosaurus(query)
        if not candidates:
            return _failure(f"Cell line '{name}' not found in Cellosaurus")

        exact = [
            c
            for c in candidates
            if cell_line_names_match(query, c.get("name", ""), c.get("synonyms", []))
        ]
        if len(exact) == 1:
            return _success(dict(exact[0]))
        if len(exact) > 1:
            accessions = ", ".join(c.get("accession", "?") for c in exact)
            return _failure(
                f"Ambiguous cell-line name '{name}' — {len(exact)} exact matches "
                f"({accessions}); refine the name or use an accession"
            )
        return _failure(
            f"No confident Cellosaurus match for cell-line name '{name}' (only partial matches)"
        )
    except TransientLookupError as exc:
        return _failure(f"Cellosaurus unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("Cellosaurus name search failed for '%s'", name)
        return _failure(f"Cellosaurus name search failed: {exc}")


@functools.lru_cache(maxsize=256)
def lookup_aop(aop_id: str) -> dict[str, Any]:
    """Look up an Adverse Outcome Pathway by its AOP-Wiki identifier.

    Args:
        aop_id: Numeric AOP identifier, e.g. "610" or 42.

    Returns:
        Standard lookup dict with keys:
            found: True if AOP was found.
            data: Dict with "aop", "events", "relationships" keys.
            error: Error message or None on success.
    """
    try:
        result = lookup_aop_wiki(str(aop_id))
        if result and result.get("aop"):
            return _success(result)
        return _failure(f"AOP '{aop_id}' not found in AOP-Wiki")
    except TransientLookupError as exc:
        return _failure(f"AOP-Wiki unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("AOP-Wiki lookup failed for '%s'", aop_id)
        return _failure(f"AOP-Wiki lookup failed: {exc}")


@functools.lru_cache(maxsize=256)
def lookup_bao_term(query: str) -> dict[str, Any]:
    """Search the BioAssay Ontology for a term matching the query.

    Args:
        query: Free-text description (e.g. "gene expression assay").

    Returns:
        Standard lookup dict with keys:
            found: True if a matching term was found.
            data: Dict with @id, @type, name, termCode keys.
            error: Error message or None on success.
    """
    try:
        result = lookup_bao_term_ols(query)
        if result and result.get("@id"):
            return _success(result)
        return _failure(f"No BAO term found for '{query}'")
    except TransientLookupError as exc:
        return _failure(f"BAO/OLS unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("BAO/OLS lookup failed for '%s'", query)
        return _failure(f"BAO/OLS lookup failed: {exc}")


@functools.lru_cache(maxsize=256)
def lookup_ontology_term(query: str, ontology: str) -> dict[str, Any]:
    """Search any OLS4-hosted ontology for a term matching the query.

    Generalises ``lookup_bao_term`` to any ontology (EFO/OBI/NCIT/UBERON/
    ChEBI/UO/…), so the agent can ground free-text annotations in the right
    vocabulary.

    Args:
        query: Free-text description (e.g. "apoptosis", "liver").
        ontology: OLS ontology short name (e.g. "efo", "obi", "chebi").

    Returns:
        Standard lookup dict with keys:
            found: True if a matching term was found.
            data: Dict with @id, @type, name, termCode, and (when available)
                  score keys.
            error: Error message or None on success.
    """
    try:
        result = lookup_ontology_term_ols(query, ontology)
        if result and result.get("@id"):
            return _success(result)
        return _failure(f"No '{ontology}' term found for '{query}'")
    except TransientLookupError as exc:
        return _failure(f"OLS unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("OLS lookup failed for '%s' in '%s'", query, ontology)
        return _failure(f"OLS lookup failed: {exc}")


@functools.lru_cache(maxsize=256)
def lookup_unit(unit_string: str) -> dict[str, Any]:
    """Resolve a unit string to a Units of Measurement Ontology (UO) IRI.

    Args:
        unit_string: Plain-text unit (e.g. "micromolar", "hour").

    Returns:
        Standard lookup dict with keys:
            found: True if a matching UO term was found.
            data: Dict with @id (UO IRI), @type, name, termCode, and (when
                  available) score keys.
            error: Error message or None on success.
    """
    try:
        result = lookup_unit_ols(unit_string)
        if result and result.get("@id"):
            return _success(result)
        return _failure(f"No UO unit found for '{unit_string}'")
    except TransientLookupError as exc:
        return _failure(f"UO/OLS unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("UO/OLS lookup failed for '%s'", unit_string)
        return _failure(f"UO/OLS lookup failed: {exc}")


@functools.lru_cache(maxsize=256)
def lookup_dtxsid(query: str) -> dict[str, Any]:
    """Resolve a chemical to its EPA DTXSID via the CompTox Dashboard.

    Args:
        query: Chemical name, CAS RN, or InChIKey (e.g. "Bisphenol A",
               "80-05-7").

    Returns:
        Standard lookup dict with keys:
            found: True if a DTXSID was resolved.
            data: Dict with dtxsid, @id, @type, name, and optionally casrn,
                  inchikey keys.
            error: Error message or None on success.
    """
    try:
        result = lookup_dtxsid_comptox(query)
        if result and result.get("dtxsid"):
            return _success(result)
        return _failure(f"No DTXSID found for '{query}' in CompTox")
    except TransientLookupError as exc:
        return _failure(f"CompTox unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("CompTox lookup failed for '%s'", query)
        return _failure(f"CompTox lookup failed: {exc}")


@functools.lru_cache(maxsize=256)
def lookup_orcid(orcid_id: str) -> dict[str, Any]:
    """Look up a person by their ORCID iD.

    Args:
        orcid_id: Bare ORCID iD, e.g. "0000-0001-6004-8653".

    A malformed iD is rejected before any network call: an ORCID iD carries an
    ISO 7064 MOD 11-2 check digit, so a transcription error is detectable
    locally. Both definitive outcomes — malformed, and well-formed but not
    registered — carry a ``fix`` naming the next action, because neither is
    recoverable by retrying and the model otherwise has no exit (see
    :func:`_failure`).

    Args:
        orcid_id: Bare ORCID iD, e.g. "0000-0001-6004-8653".

    Returns:
        Standard lookup dict with keys:
            found: True if ORCID record was found (always True for valid
                   ORCID format; ORCID returns a minimal fallback).
            data: Dict with @id, @type, identifier, name, givenName,
                  familyName, affiliation_name, affiliation_ror keys.
            error: Error message or None on success.
            fix: On a definitive failure, what to do instead.
    """
    if not is_well_formed_orcid(orcid_id):
        # Never guess a correction: the digits identify a real person, and the
        # nearest valid iD belongs to someone else.
        return _failure(
            f"'{orcid_id}' is not a valid ORCID iD — it fails the ORCID check digit, "
            "so it was mistyped or misread rather than deregistered. No lookup was "
            "attempted, and re-running this will not change the answer.",
            fix=(
                "Do not guess a corrected iD. Re-read it from the source document; if "
                "it still does not check out, draft the person without one — "
                "draft_person(name='...') with no orcid hint is valid and complete. "
                "Ask the human with present_to_human only if this identifier has to be "
                "in the crate."
            ),
        )
    try:
        result = lookup_orcid_api(orcid_id)
        if result and "name" in result:
            return _success(result)
        # ORCID returns a fallback dict even on 404, so check for @id
        if result and result.get("@id"):
            return _success(result)
        return _failure(
            f"ORCID has no public record for '{orcid_id}'. The iD is well formed, so "
            "this is a definitive not-found (unregistered, or the record is private) "
            "and not a temporary outage — repeating this lookup returns the same result.",
            fix=(
                "Draft the person without one — draft_person(name='...') with no orcid "
                "hint is valid and complete. Ask the human with present_to_human only if "
                "this identifier has to be in the crate."
            ),
        )
    except TransientLookupError as exc:
        return _failure(f"ORCID unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("ORCID lookup failed for '%s'", orcid_id)
        return _failure(f"ORCID lookup failed: {exc}")


@functools.lru_cache(maxsize=256)
def lookup_ror(name: str) -> dict[str, Any]:
    """Search the Research Organization Registry for an organization by name.

    Args:
        name: Organization name (e.g. "Maastricht University").

    Returns:
        Standard lookup dict with keys:
            found: True if an organization was found.
            data: Dict with @id, @type, name, url, identifier keys.
            error: Error message or None on success.
    """
    try:
        result = search_ror(name)
        if result and result.get("@id"):
            return _success(result)
        return _failure(f"No ROR organization found for '{name}'")
    except TransientLookupError as exc:
        return _failure(f"ROR unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("ROR lookup failed for '%s'", name)
        return _failure(f"ROR lookup failed: {exc}")


@functools.lru_cache(maxsize=256)
def lookup_doi(doi: str) -> dict[str, Any]:
    """Look up a publication by its DOI via Crossref.

    Args:
        doi: DOI string (e.g. "10.1016/j.tox.2021.152898").

    Returns:
        Standard lookup dict with keys:
            found: True if the DOI was resolved.
            data: Dict with @id, @type, name, headline, author,
                  datePublished, url, identifier keys.
            error: Error message or None on success.
    """
    try:
        result = lookup_doi_crossref(doi)
        if result and result.get("name"):
            return _success(result)
        return _failure(f"DOI '{doi}' not found in Crossref")
    except TransientLookupError as exc:
        return _failure(f"Crossref unavailable (transient): {exc}", transient=True)
    except Exception as exc:
        logger.exception("Crossref lookup failed for '%s'", doi)
        return _failure(f"Crossref lookup failed: {exc}")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("lookup_compound", lookup_compound, takes_state=False)
TOOL_REGISTRY.register("lookup_cell_line", lookup_cell_line, takes_state=False)
TOOL_REGISTRY.register("lookup_cell_line_by_name", lookup_cell_line_by_name, takes_state=False)
TOOL_REGISTRY.register("lookup_aop", lookup_aop, takes_state=False)
TOOL_REGISTRY.register("lookup_bao_term", lookup_bao_term, takes_state=False)
TOOL_REGISTRY.register("lookup_ontology_term", lookup_ontology_term, takes_state=False)
TOOL_REGISTRY.register("lookup_unit", lookup_unit, takes_state=False)
TOOL_REGISTRY.register("lookup_dtxsid", lookup_dtxsid, takes_state=False)
TOOL_REGISTRY.register("lookup_orcid", lookup_orcid, takes_state=False)
TOOL_REGISTRY.register("lookup_ror", lookup_ror, takes_state=False)
TOOL_REGISTRY.register("lookup_doi", lookup_doi, takes_state=False)
