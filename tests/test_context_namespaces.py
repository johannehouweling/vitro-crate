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

import re
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
    "http://aopkb.org/aop_ontology#": "AOPO, the Adverse Outcome Pathway Ontology",
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


def test_the_aop_terms_are_aopo() -> None:
    """The specific claim #644 fixes, pinned by name.

    Our property names were AOPO's all along — `has_key_event`,
    `has_adverse_outcome` are spelled exactly as AOPO spells them — so this is
    the vocabulary the crate was already using, finally said out loud.
    """
    context = ISA_TOX_CONTEXT[0]
    aopo = "http://aopkb.org/aop_ontology#"

    assert context["AdverseOutcomePathway"] == aopo + "AdverseOutcomePathway"
    assert context["KeyEvent"] == aopo + "KeyEvent"
    assert context["KeyEventRelationship"] == aopo + "KeyEventRelationship"
    assert context["has_key_event"] == aopo + "has_key_event"
    assert context["has_adverse_outcome"] == aopo + "has_adverse_outcome"
    assert context["has_molecular_initiating_event"] == aopo + "has_molecular_initiating_event"
    assert context["has_key_event_relationship"] == aopo + "has_key_event_relationship"
    assert context["upstream_event"] == aopo + "has_upstream_key_event"
    assert context["downstream_event"] == aopo + "has_downstream_key_event"


def test_no_term_still_points_at_the_invented_namespace() -> None:
    """Belt and braces: the allow-list catches a namespace nobody declared, and
    this catches the one we know was wrong, wherever it might survive."""
    assert not [iri for iri in _iris() if "aopwiki.org/ontology" in iri]


def test_the_shapes_use_the_same_namespace_as_the_context() -> None:
    """A shape asking for `sh:class aopo:AdverseOutcomePathway` under one IRI
    while the crate types the entity under another validates nothing — quietly,
    because an unmatched target is a rule that never runs."""
    import pathlib

    shapes = pathlib.Path(__file__).resolve().parents[1] / "profiles" / "shapes" / "tox"
    for ttl in shapes.glob("*.ttl"):
        text = ttl.read_text(encoding="utf-8")
        for prefix_iri in re.findall(r"@prefix\s+aopo?w?i?k?i?:\s+<([^>]+)>", text):
            assert prefix_iri == "http://aopkb.org/aop_ontology#", f"{ttl.name}: {prefix_iri}"


def test_a_crate_built_now_satisfies_the_aop_shapes() -> None:
    """The claim the textual check above cannot make.

    A shape asking for ``sh:class aopo:AdverseOutcomePathway`` matches only if
    the crate's own ``@context`` expands its ``AdverseOutcomePathway`` type to
    that same IRI. Get the two out of step and the rule does not fail — it finds
    no target and never runs, which is the quiet failure this repo cares about
    most. So this drives the real validator over a crate carrying the real
    context, and asserts the AOP rules stay silent because they are satisfied.
    """
    from profiles.validator import validate_crate_dict

    crate = {
        "@context": ISA_TOX_CONTEXT,
        "@graph": [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "An investigation",
                "description": "A crate that situates its biology in an AOP.",
                "hasPart": [{"@id": "#study"}],
            },
            {
                "@id": "#study",
                "@type": "Dataset",
                "additionalType": "Study",
                "name": "A study",
                "description": "A study that names its pathway.",
                "hasPart": [{"@id": "#assay"}],
                "mentions": [{"@id": "https://aopwiki.org/aops/610"}],
            },
            {
                "@id": "#assay",
                "@type": "Dataset",
                "additionalType": "Assay",
                "name": "An assay",
                "description": "An assay that names the event it measures.",
                "mentions": [{"@id": "https://aopwiki.org/events/2258"}],
            },
            {
                "@id": "https://aopwiki.org/aops/610",
                "@type": ["AdverseOutcomePathway", "schema:DefinedTerm"],
                "name": "Decreased thyroid hormone levels in the brain",
            },
            {
                "@id": "https://aopwiki.org/events/2258",
                "@type": ["KeyEvent", "schema:DefinedTerm"],
                "name": "Inhibition, monocarboxylate transporter 8",
            },
        ],
    }

    messages = [
        issue.message or ""
        for result in validate_crate_dict(crate, severity="recommended", profile="tox")
        for issue in result.issues
    ]

    assert not [m for m in messages if "Adverse Outcome Pathway" in m], messages
    assert not [m for m in messages if "Key Event" in m], messages
