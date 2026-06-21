"""
BioAssay Ontology (BAO) lookup via EBI OLS4 API.

Used to annotate assay measurement methods, measurement techniques,
and lab equipment with standardised ontology terms.
"""

from __future__ import annotations

import functools
import time

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://www.ebi.ac.uk/ols4/api/search"


@functools.lru_cache(maxsize=512)
def lookup_bao_term(query: str) -> dict:
    """Search BAO for the best matching term.

    Args:
        query: plain-text description, e.g. "gene expression assay", "RT-qPCR",
               "QuantStudio 7 Flex"

    Returns:
        dict with keys: @id (BAO IRI), @type ("DefinedTerm"), name.
        Returns {} if no match. Raises TransientLookupError on a transient API
        failure (timeout / connection / 429 / 5xx).
    """
    if not query:
        return {}
    try:
        time.sleep(0.1)
        data = http_get_json(
            _BASE,
            params={"q": query, "ontology": "bao", "rows": 1},
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

        return {
            "@id": iri,
            "@type": "DefinedTerm",
            "name": label,
            "termCode": top.get("short_form", ""),
        }
    except TransientLookupError:
        raise
    except Exception:
        return {}
