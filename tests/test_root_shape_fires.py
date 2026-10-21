"""The strict-Investigation shape must actually select the root data entity.

`9_investigation_strict.ttl` targets `ro-crate:RootDataEntity`, and its prefix
resolves to `…/profiles/ro-crate/` while the class is defined by the base profile
under `…/profiles/ro-crate/1.2/`. It works — SHACL resolves it through the
profile's ontology rather than by string concatenation — but nothing proved it,
and a `sh:targetClass` that matches nothing is not an error in SHACL: the shape
simply never runs, and every crate passes.

That failure mode is live upstream right now. crs4/rocrate-validator#193 reports
the base profile declaring two different namespaces for its own 1.2 shapes and
says validation outcomes become "kind of random" when a profile picks the wrong
one. If that churn ever moves the class out from under us, this test is what says
so, instead of a REQUIRED gate quietly passing everything.

Deliberately built from a bare root rather than from a drafted one: the drafters
fill name, description, license and datePublished with defaults, so a crate built
the normal way has nothing for this shape to find and cannot tell a working shape
from a dead one.
"""

from __future__ import annotations

import pytest

BARE_ROOT = {
    "@context": "https://w3id.org/ro/crate/1.2/context",
    "@graph": [
        {
            "@id": "ro-crate-metadata.json",
            "@type": "CreativeWork",
            "conformsTo": {"@id": "https://w3id.org/ro/crate/1.2"},
            "about": {"@id": "./"},
        },
        {"@id": "./", "@type": "Dataset", "additionalType": "Investigation"},
    ],
}

REQUIRED_ON_THE_ROOT = (
    "schema:name",
    "schema:description",
    "schema:license",
    "schema:datePublished",
)


@pytest.fixture(scope="module")
def messages() -> list[str]:
    from profiles.validator import validate_crate_dict

    results = validate_crate_dict(BARE_ROOT, severity="required", profile="tox")
    return [issue.message for result in results for issue in result.issues]


def test_the_shape_selects_the_root_at_all(messages):
    assert messages, (
        "the strict-Investigation shape found nothing on a root with no name, "
        "description, licence or date — its target is selecting no node, so the "
        "REQUIRED gate is passing every crate"
    )


@pytest.mark.parametrize("field", REQUIRED_ON_THE_ROOT)
def test_each_required_descriptor_is_enforced(messages, field):
    assert any(field in message for message in messages), (
        f"nothing required {field} of the root data entity"
    )


EXPOSURE_RULE = "MUST include an Exposure"


def _exposure_crate(about: list[dict]) -> dict:
    """The root's one Assay, and an Exposure the Assay is ``about`` or not."""
    descriptor, _root = BARE_ROOT["@graph"]
    return {
        "@context": BARE_ROOT["@context"],
        "@graph": [
            descriptor,
            {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#assay"}]},
            {"@id": "#assay", "@type": "Dataset", "additionalType": "Assay", "about": about},
            {
                "@id": "#exposure",
                "@type": "https://bioschemas.org/LabProcess",
                "additionalType": "Exposure",
            },
        ],
    }


def test_a_crate_is_toxicology_only_if_its_root_reaches_an_exposure(messages):
    """#781: every other tox shape targets a class, so a crate with no Exposure
    passed them all. This one targets the root: it fires on the bare root and on
    a detached Exposure, and names no property, since no field on the root
    answers it. Hanging the Exposure off its Assay clears it."""
    from profiles.validator import validate_crate_dict

    def fired(doc: dict) -> list[tuple[str | None, str | None]]:
        return [
            (issue.entity_id, issue.property)
            for result in validate_crate_dict(doc, severity="required", profile="tox")
            for issue in result.issues
            if EXPOSURE_RULE in issue.message
        ]

    assert any(EXPOSURE_RULE in message for message in messages), messages
    assert fired(_exposure_crate(about=[])) == [("./", None)]
    assert fired(_exposure_crate(about=[{"@id": "#exposure"}])) == []
