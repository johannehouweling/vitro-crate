"""
BioAssay Ontology (BAO) lookup via EBI OLS4 API.

Used to annotate assay measurement methods, measurement techniques,
and lab equipment with standardised ontology terms.
"""

from __future__ import annotations

import functools
import time

import requests

_BASE = "https://www.ebi.ac.uk/ols4/api/search"


@functools.lru_cache(maxsize=512)
def lookup_bao_term(query: str) -> dict:
    """Search BAO for the best matching term.

    Args:
        query: plain-text description, e.g. "gene expression assay", "RT-qPCR",
               "QuantStudio 7 Flex"

    Returns:
        dict with keys: @id (BAO IRI), @type ("DefinedTerm"), name.
        Returns {} if no match or on API failure.
    """
    if not query:
        return {}
    try:
        time.sleep(0.1)
        r = requests.get(
            _BASE,
            params={"q": query, "ontology": "bao", "rows": 1},
            timeout=10,
        )
        if r.status_code != 200:
            return {}

        docs = r.json().get("response", {}).get("docs", [])
        if not docs:
            return {}

        top = docs[0]
        iri = top.get("iri", "")
        label = top.get("label", query)

        if not iri:
            return {}

        return {
            "@id": iri,
            "@type": "DefinedTerm",
            "name": label,
            "termCode": top.get("short_form", ""),
        }
    except Exception:
        return {}
