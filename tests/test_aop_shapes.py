"""The AOP and Key Event shapes check the AOP-Wiki link itself.

A Study SHOULD mention its pathway as ``https://aopwiki.org/aops/<id>`` and an
Assay the event it measures as ``https://aopwiki.org/events/<id>``. The link is
the claim, so the shapes read the IRI, not a type: a bare link passes, and a
node typed ``AdverseOutcomePathway`` or ``KeyEvent`` under any other identifier
does not.
"""

from __future__ import annotations

from profiles.context import ISA_TOX_CONTEXT
from profiles.validator import validate_crate_dict

AOP = "https://aopwiki.org/aops/610"
KE = "https://aopwiki.org/events/2258"

# Each focus node mentions one thing; only the "-link" nodes name the right kind
# of AOP-Wiki IRI.
STUDIES = {
    "#study-link": {"@id": AOP},
    "#study-typed-local": {"@id": "#local-aop"},
    "#study-names-an-event": {"@id": KE},
    "#study-resolver-iri": {"@id": "https://identifiers.org/aop/610"},
    "#study-plain-string": AOP,
}
ASSAYS = {
    "#assay-link": {"@id": KE},
    "#assay-typed-local": {"@id": "#local-ke"},
    "#assay-names-a-pathway": {"@id": AOP},
}


def _dataset(node_id: str, kind: str, mentions, **extra) -> dict:
    return {
        "@id": node_id,
        "@type": "Dataset",
        "additionalType": kind,
        "name": node_id,
        "description": f"A {kind.lower()}.",
        "mentions": mentions,
        **extra,
    }


def _crate() -> dict:
    assays = [{"@id": a} for a in ASSAYS]
    studies = [
        _dataset(s, "Study", m, **({"hasPart": assays} if s == "#study-link" else {}))
        for s, m in STUDIES.items()
    ]
    return {
        "@context": ISA_TOX_CONTEXT,
        "@graph": [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "An investigation",
                "description": "Studies that name their pathway in different ways.",
                "hasPart": [{"@id": s} for s in STUDIES],
            },
            *studies,
            *(_dataset(a, "Assay", m) for a, m in ASSAYS.items()),
            {
                "@id": "#local-aop",
                "@type": ["AdverseOutcomePathway", "schema:DefinedTerm"],
                "name": "A pathway typed as one, with no AOP-Wiki IRI",
            },
            {
                "@id": "#local-ke",
                "@type": ["KeyEvent", "schema:DefinedTerm"],
                "name": "An event typed as one, with no AOP-Wiki IRI",
            },
        ],
    }


def test_only_an_aop_wiki_link_of_the_right_kind_satisfies_the_shapes() -> None:
    (result,) = validate_crate_dict(_crate(), severity="recommended", profile="tox")

    def warned(words: str) -> set[str]:
        return {
            (i.entity_id or "").removeprefix("./")
            for i in result.issues
            if words in (i.message or "")
        }

    assert warned("Adverse Outcome Pathway") == set(STUDIES) - {"#study-link"}
    assert warned("Key Event") == set(ASSAYS) - {"#assay-link"}
