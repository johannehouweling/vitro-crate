"""Canonical ontology-term IRIs — the single source of truth (issue #358).

Ontology term IRIs (BAO, NCIT, EFO, MSIO, CHEBI, PATO, IAO, OBI, UO, …) are built
here by CURIE expansion so every emitter — ``profiles/models/tox.py``,
``builder/tools/_crate_mapping.py``, ``profiles/context.py``,
``builder/tools/drafters.py``, ``builder/agents/react/tools_spec.py`` — resolves a
given term to exactly one IRI. Terms are referenced by CURIE, e.g.
``iri("NCIT:C83280")``; each prefix's namespace is defined once, in :data:`PREFIXES`.

Best practice: a term's identity IRI is the ontology's **own canonical, resolvable**
IRI — never a meta-resolver URL (``bioregistry.io`` / ``identifiers.org``), which is
for *looking things up*, not for *being* the identifier. OBO Foundry ontologies use the
OBO PURL; BAO uses its ``bioassayontology.org`` namespace; EFO uses its EBI namespace.
This is also what the OLS lookup path emits, so a term gets the same IRI whether it is
hardcoded here or resolved online.
"""

from __future__ import annotations

# CURIE prefix -> namespace, up to and including the ``<PREFIX>_`` (or ``#<PREFIX>_``)
# separator, so ``PREFIXES[p] + local`` is the full term IRI.
PREFIXES: dict[str, str] = {
    # BioAssay Ontology — native namespace (not in the OBO PURL system)
    "BAO": "http://www.bioassayontology.org/bao#BAO_",
    # Experimental Factor Ontology — EBI native namespace
    "EFO": "http://www.ebi.ac.uk/efo/EFO_",
    # OBO Foundry ontologies — canonical OBO PURLs
    "CHEBI": "http://purl.obolibrary.org/obo/CHEBI_",
    "IAO": "http://purl.obolibrary.org/obo/IAO_",
    "MSIO": "http://purl.obolibrary.org/obo/MSIO_",
    "NCIT": "http://purl.obolibrary.org/obo/NCIT_",
    "OBI": "http://purl.obolibrary.org/obo/OBI_",
    "PATO": "http://purl.obolibrary.org/obo/PATO_",
    "UO": "http://purl.obolibrary.org/obo/UO_",
}


def iri(curie: str) -> str:
    """Expand a CURIE (e.g. ``"NCIT:C83280"``) to its canonical ontology term IRI.

    Raises ``KeyError`` for an unregistered prefix or a malformed CURIE — a typo, or a
    new ontology that must be added to :data:`PREFIXES` first (keeping IRIs
    single-sourced rather than pasted inline).
    """
    prefix, sep, local = curie.partition(":")
    if not sep or not local or prefix not in PREFIXES:
        raise KeyError(f"Unknown or malformed ontology CURIE: {curie!r}")
    return PREFIXES[prefix] + local
