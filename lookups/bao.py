"""
Generic ontology-term lookup via the EBI OLS4 API.

OLS4 hosts dozens of ontologies behind a single search endpoint, so the same
parser serves BAO (BioAssay Ontology) assay annotations, the Units of
Measurement Ontology (UO) for quantities, and any other OLS-hosted ontology
(EFO/OBI/NCIT/UBERON/ChEBI, …).

``lookup_ontology_term`` is the generic primitive; ``lookup_bao_term`` and
``lookup_unit`` are thin, ontology-pinned wrappers over it. ``lookup_exact_term``
answers the other question: the one term a given word names, by its label or an
exact synonym.
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


@functools.lru_cache(maxsize=1024)
def lookup_exact_term(label: str, ontology: str) -> dict:
    """The one term of *ontology* that the word *label* names.

    :func:`lookup_ontology_term` returns the best match for a description, and
    the search finds some term for nearly any word. This asks a narrower
    question, the only one whose answer may stand beside a stated value: does
    the word name a term of this ontology? It does when exactly one of the
    ontology's own, current terms has it as its label, or else when exactly one
    has it as a synonym the ontology itself marks as exact. A narrow, broad or
    related synonym names something else and is not accepted. A plural that
    names nothing is asked about once more as its singular ("Dopaminergic
    neurons"). The service is asked for an exact match and the answer is checked
    again here.

    Args:
        label: the word as stated, e.g. "Brain".
        ontology: OLS ontology short name, e.g. "uberon", "cl".

    Returns:
        dict with keys @id (term IRI), @type ("DefinedTerm"), name (the term's
        label) and termCode (its CURIE). {} when the word names no term, or more
        than one. Raises TransientLookupError on a transient API failure.
    """
    word = " ".join(str(label or "").split())
    if not word or not ontology:
        return {}
    found = _named(word, ontology)
    if not found and len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        found = _named(word[:-1], ontology)
    return found


def _named(word: str, ontology: str) -> dict:
    """The term of *ontology* whose label, or else whose exact synonym, is *word*."""
    data = http_get_json(
        _BASE,
        params={
            "q": word,
            "ontology": ontology,
            "exact": "true",
            "queryFields": "label,synonym",
            "local": "true",
            "obsoletes": "false",
            "rows": 20,
            "fieldList": "iri,label,short_form,obo_id,exact_synonyms",
        },
    )
    response = data.get("response") if isinstance(data, dict) else None
    docs = response.get("docs") if isinstance(response, dict) else None
    prefix = f"{ontology.upper()}_"
    own = [
        doc
        for doc in docs or []
        if isinstance(doc, dict)
        and doc.get("iri")
        and doc.get("label")
        and str(doc.get("short_form") or "").startswith(prefix)
    ]
    wanted = _folded(word)
    by_label = [doc for doc in own if _folded(doc["label"]) == wanted]
    by_synonym = [doc for doc in own if wanted in map(_folded, doc.get("exact_synonyms") or [])]
    named = by_label or by_synonym
    if len({doc["iri"] for doc in named}) != 1:
        return {}
    term = named[0]
    return {
        "@id": term["iri"],
        "@type": "DefinedTerm",
        "name": " ".join(str(term["label"]).split()),
        "termCode": term.get("obo_id") or str(term["short_form"]).replace("_", ":", 1),
    }


def _folded(text: object) -> str:
    """*text* with case and spacing set aside, the way a name is compared."""
    return " ".join(str(text or "").split()).casefold()


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
