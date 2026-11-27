"""Every namespace the crate's ``@context`` declares must be a real one (#644).

A JSON-LD context is a set of claims about what the crate's terms *mean*, and a
term under a namespace nobody publishes means nothing — it reads as a private
vocabulary that merely looks familiar. Ten AOP terms shipped for months under
``https://aopwiki.org/ontology/``, a path AOP-Wiki does not serve, while the
property names themselves were AOPO's all along.

The rule these hold the context to is the one `profiles/ontology_iris` already
states: a term's identity IRI is the ontology's own canonical IRI.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from profiles.context import ISA_TOX_CONTEXT

# Every namespace the context is allowed to draw terms from, and what it is.
# An allow-list rather than a live HTTP check: a test that reaches the network
# fails on a train, and an ontology host being down is not a defect in this
# crate. Adding an entry is the deliberate act — which is exactly what nobody
# did for `aopwiki.org/ontology`, because nothing asked them to.
KNOWN_NAMESPACES: dict[str, str] = {
    "http://schema.org/": "schema.org (HTTP, to match the RO-Crate context)",
    "https://bioschemas.org/": "Bioschemas types",
    # Bioschemas splits its vocabulary: types at the bare namespace, properties
    # beneath /properties/ — see `context.BIOSCHEMAS_PROP`.
    "https://bioschemas.org/properties/": "Bioschemas properties",
    "http://purl.org/dc/terms/": "DCMI Metadata Terms",
    "http://www.w3.org/2000/01/rdf-schema#": "RDF Schema",
    "http://www.w3.org/2004/02/skos/core#": "SKOS",
    "http://www.w3.org/ns/csvw#": "CSV on the Web",
    "http://www.geneontology.org/formats/oboInOwl#": "oboInOwl",
    "http://purl.obolibrary.org/obo/": "OBO Foundry PURLs",
    "http://www.bioassayontology.org/bao#": "BioAssay Ontology",
    "http://www.ebi.ac.uk/efo/": "Experimental Factor Ontology",
}


def _iris() -> set[str]:
    """Every absolute IRI the context maps a term to."""
    found: set[str] = set()
    for block in ISA_TOX_CONTEXT:
        for value in block.values():
            target = value.get("@id") if isinstance(value, dict) else value
            if isinstance(target, str) and target.startswith(("http://", "https://")):
                found.add(target)
    return found


def _namespace(iri: str) -> str:
    """The IRI up to and including its final separator."""
    if "#" in iri:
        return iri.split("#")[0] + "#"
    parts = urlsplit(iri)
    return f"{parts.scheme}://{parts.netloc}{parts.path.rsplit('/', 1)[0]}/"


def test_every_term_comes_from_a_declared_namespace() -> None:
    unknown = sorted(
        {_namespace(iri) for iri in _iris()} - set(KNOWN_NAMESPACES)
    )

    assert unknown == [], (
        "the context declares terms under namespaces nobody has vouched for: "
        + ", ".join(unknown)
        + ". If one is real, add it to KNOWN_NAMESPACES with what it is; if it is "
        "invented, the terms under it mean nothing to a reader of the crate."
    )


def test_the_context_declares_no_aop_term() -> None:
    """The crate states the pathway a Study addresses and the key event an Assay
    measures as DefinedTerms at their AOP-Wiki address, and nothing else of a
    pathway, so it declares no AOP class and no AOP property."""
    context = ISA_TOX_CONTEXT[0]

    for term in (
        "AdverseOutcomePathway",
        "KeyEvent",
        "KeyEventRelationship",
        "aopWikiStressorId",
        "eventType",
        "has_key_event",
        "has_adverse_outcome",
        "has_molecular_initiating_event",
        "has_key_event_relationship",
        "upstream_event",
        "downstream_event",
    ):
        assert term not in context, term
    assert not [t for t, iri in context.items() if "aopkb.org" in str(iri)]


def test_no_term_still_points_at_the_invented_namespace() -> None:
    """Belt and braces: the allow-list catches a namespace nobody declared, and
    this catches the one we know was wrong, wherever it might survive."""
    assert not [iri for iri in _iris() if "aopwiki.org/ontology" in iri]
