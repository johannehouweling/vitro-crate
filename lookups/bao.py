"""
Generic ontology-term lookup via the EBI OLS4 API.

OLS4 hosts dozens of ontologies behind a single search endpoint, so the same
parser serves BAO (BioAssay Ontology) assay annotations, the Units of
Measurement Ontology (UO) for quantities, and any other OLS-hosted ontology
(EFO/OBI/NCIT/UBERON/ChEBI, …).

``lookup_ontology_term`` is the generic primitive; ``lookup_bao_term`` and
``lookup_unit`` are thin, ontology-pinned wrappers over it.
"""

from __future__ import annotations

import functools

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://www.ebi.ac.uk/ols4/api/search"


@functools.lru_cache(maxsize=1024)
def lookup_ontology_term(query: str, ontology: str, rows: int = 1) -> dict:
    """Search an OLS4-hosted ontology for the best matching term.

    Args:
        query: plain-text description, e.g. "gene expression assay", "RT-qPCR",
               "micromolar".
        ontology: OLS ontology short name, e.g. "bao", "uo", "efo", "chebi".
        rows: number of OLS rows to request (only the top hit is returned).

    Returns:
        dict with keys: @id (term IRI), @type ("DefinedTerm"), name, termCode,
        and ``score`` (the OLS4 relevance score, when present). Returns {} if no
        match. Raises TransientLookupError on a transient API failure
        (timeout / connection / 429 / 5xx).
    """
    if not query or not ontology:
        return {}
    try:
        # Politeness throttling is handled centrally in http_get_json (#62).
        data = http_get_json(
            _BASE,
            params={"q": query, "ontology": ontology, "rows": rows},
        )
        if data is NOT_FOUND:
            return {}

        docs = data.get("response", {}).get("docs", [])
        if not docs:
            return {}

        top = docs[0]
        iri = top.get("iri", "")
        label = top.get("label", query)

        if not iri:
            return {}

        result = {
            "@id": iri,
            "@type": "DefinedTerm",
            "name": label,
            "termCode": top.get("short_form", ""),
        }
        # OLS4 returns a Solr relevance score on each doc; surface it as a
        # confidence signal when available.
        score = top.get("score")
        if score is not None:
            result["score"] = score
        return result
    except TransientLookupError:
        raise
    except Exception:
        return {}


def lookup_bao_term(query: str) -> dict:
    """Search BAO for the best matching term (back-compat wrapper).

    Thin wrapper over :func:`lookup_ontology_term` pinned to ``ontology="bao"``.

    Args:
        query: plain-text description, e.g. "gene expression assay", "RT-qPCR",
               "QuantStudio 7 Flex"

    Returns:
        dict with keys: @id (BAO IRI), @type ("DefinedTerm"), name, termCode.
        Returns {} if no match. Raises TransientLookupError on a transient API
        failure (timeout / connection / 429 / 5xx).
    """
    return lookup_ontology_term(query, "bao")


def lookup_unit(unit_string: str) -> dict:
    """Resolve a unit string to a Units of Measurement Ontology (UO) IRI.

    Thin wrapper over :func:`lookup_ontology_term` pinned to ``ontology="uo"``.

    Args:
        unit_string: plain-text unit, e.g. "micromolar", "hour", "milligram".

    Returns:
        dict with keys: @id (UO IRI), @type ("DefinedTerm"), name, termCode, and
        ``score`` when available. Returns {} if no match. Raises
        TransientLookupError on a transient API failure.
    """
    return lookup_ontology_term(unit_string, "uo")
