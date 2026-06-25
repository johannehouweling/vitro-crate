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
import time
from typing import Any

from lookups._http import TransientLookupError
from lookups.aopwiki import lookup_aop as lookup_aop_wiki
from lookups.bao import lookup_bao_term as lookup_bao_term_ols
from lookups.bao import lookup_ontology_term as lookup_ontology_term_ols
from lookups.bao import lookup_unit as lookup_unit_ols
from lookups.cellosaurus import lookup_cellosaurus
from lookups.comptox import lookup_dtxsid as lookup_dtxsid_comptox
from lookups.crossref import lookup_doi as lookup_doi_crossref
from lookups.orcid import lookup_orcid as lookup_orcid_api
from lookups.pubchem import lookup_pubchem
from lookups.ror import search_ror

logger = logging.getLogger(__name__)

# Standard return type for all lookup functions
LOOKUP_RESULT = dict[str, Any]  # {"found": bool, "data": dict, "error": str | None}


def _success(data: dict) -> dict[str, Any]:
    """Wrap successful lookup data into the standard result format."""
    return {"found": True, "data": data, "error": None}


def _failure(error: str, transient: bool = False) -> dict[str, Any]:
    """Wrap a failure into the standard result format.

    ``transient=True`` marks a temporary outage (timeout / 429 / 5xx) as
    distinct from a definitive not-found, so callers (e.g. verify_identifier)
    can keep the user's value and retry rather than treat it as unresolved.
    """
    return {"found": False, "data": {}, "error": error, "transient": transient}


@functools.lru_cache(maxsize=256)
def lookup_compound(name: str) -> dict[str, Any]:
    """Look up a chemical compound by name via PubChem.

    Args:
        name: Compound name (e.g. "Silychristin A").

    Returns:
        Standard lookup dict with keys:
            found: True if compound was found.
            data: Dict with cas, smiles, inchikey, inchi, formula, mass,
                  iupac_name, pubchem_cid keys (or empty dict).
            error: Error message or None on success.
    """
    time.sleep(0.05)
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
    time.sleep(0.05)
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
    time.sleep(0.05)
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
    time.sleep(0.05)
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
    time.sleep(0.05)
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
    time.sleep(0.05)
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
    time.sleep(0.05)
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

    Returns:
        Standard lookup dict with keys:
            found: True if ORCID record was found (always True for valid
                   ORCID format; ORCID returns a minimal fallback).
            data: Dict with @id, @type, identifier, name, givenName,
                  familyName, affiliation_name, affiliation_ror keys.
            error: Error message or None on success.
    """
    time.sleep(0.05)
    try:
        result = lookup_orcid_api(orcid_id)
        if result and "name" in result:
            return _success(result)
        # ORCID returns a fallback dict even on 404, so check for @id
        if result and result.get("@id"):
            return _success(result)
        return _failure(f"ORCID lookup failed for '{orcid_id}'")
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
    time.sleep(0.05)
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
    time.sleep(0.05)
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
TOOL_REGISTRY.register("lookup_aop", lookup_aop, takes_state=False)
TOOL_REGISTRY.register("lookup_bao_term", lookup_bao_term, takes_state=False)
TOOL_REGISTRY.register("lookup_ontology_term", lookup_ontology_term, takes_state=False)
TOOL_REGISTRY.register("lookup_unit", lookup_unit, takes_state=False)
TOOL_REGISTRY.register("lookup_dtxsid", lookup_dtxsid, takes_state=False)
TOOL_REGISTRY.register("lookup_orcid", lookup_orcid, takes_state=False)
TOOL_REGISTRY.register("lookup_ror", lookup_ror, takes_state=False)
TOOL_REGISTRY.register("lookup_doi", lookup_doi, takes_state=False)
