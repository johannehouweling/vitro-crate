"""The Profile Crate must pass what the RO-Crate Profile Portal actually checks (#856).

The portal's CI runs three gates (`scripts/validate_profiles.py` and
`scripts/upload_profiles.py` in eScienceLab/RO-Crate-Profile-Portal):

1. the URL answers with a JSON content type — nothing to test here, GitHub
   Pages serves `.json` as `application/json`;
2. `detect_profiles` finds a profile and `validate` in `metadata_only` mode
   reports no issues;
3. the parsed graph holds a ``prof:Profile``-typed subject, or the upload
   raises.

Gate 2 is :func:`validate_crate_dict` with ``profile="base"`` here — the same
rocrate-validator, offline. Gate 3 is asserted structurally: ``Profile`` in the
root's ``@type`` plus the context mapping that turns it into ``prof:Profile``,
which is what rdflib derives. Parsing it with rdflib instead would fetch the
remote context and make the suite network-dependent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from profiles.validator import validate_crate_dict
from scripts.gen_profile_crate import (
    CC_BY,
    DATE_PUBLISHED,
    KEYWORDS,
    ORCID,
    PAGES,
    PARENT_IRI,
    MIT,
    PROFILE_IRI,
    RO_CRATE,
    ROLE,
    VERSION,
    build,
)

REPO = Path(__file__).resolve().parents[1]
PROF_PROFILE = "http://www.w3.org/ns/dx/prof/Profile"


@pytest.fixture(scope="module")
def doc(tmp_path_factory) -> dict:
    return build(tmp_path_factory.mktemp("profile_crate"))


def _node(doc: dict, entity_id: str) -> dict:
    return next(n for n in doc["@graph"] if n["@id"] == entity_id)


class TestPortalGates:
    def test_the_descriptor_conforms_to_ro_crate_1_2(self, doc):
        """`detect_profiles` finds nothing without this, and the PR fails."""
        descriptor = _node(doc, "ro-crate-metadata.json")
        assert descriptor["conformsTo"] == {"@id": "https://w3id.org/ro/crate/1.2"}
        assert descriptor["about"] == {"@id": PROFILE_IRI}

    def test_it_validates_clean_against_the_base_profile(self, doc):
        (result,) = validate_crate_dict(doc, severity="required", profile="base")
        assert result.issues == [], [(i.entity_id, i.message) for i in result.issues]

    def test_the_root_is_a_prof_profile(self, doc):
        root = _node(doc, PROFILE_IRI)
        assert "Profile" in root["@type"]
        assert "Dataset" in root["@type"]

    def test_the_context_maps_profile_to_the_prof_class(self, doc):
        """What rdflib resolves ``Profile`` to in the portal's upload step."""
        local = REPO / "profiles" / "contexts" / "ro-crate-1.2-context.jsonld"
        terms = json.loads(local.read_text(encoding="utf-8"))["@context"]
        assert terms["Profile"] == PROF_PROFILE


class TestProfileCrateRequirements:
    """RO-Crate 1.2 §Profile Crate."""

    def test_the_root_declares_its_lineage_and_version(self, doc):
        root = _node(doc, PROFILE_IRI)
        # Most specific first: ISA-Tox extends ISA, which extends RO-Crate.
        assert root["isProfileOf"] == [{"@id": PARENT_IRI}, {"@id": RO_CRATE}]
        assert root["version"] == VERSION
        assert root["datePublished"] == DATE_PUBLISHED
        assert root["name"]
        assert root["description"]

    def test_it_carries_every_field_the_portal_renders(self, doc):
        """The portal's profile query (`SparqlQueriesProfiles.js`) reads these
        seven off the root; its `scripts/gaps.ttl` exists only to hand-patch
        them into listed profiles that omit them. `keywords` also drives a
        facet, so a profile without it is unfilterable there.
        """
        root = _node(doc, PROFILE_IRI)
        for field in (
            "name",
            "description",
            "identifier",
            "version",
            "datePublished",
            "author",
            "keywords",
        ):
            assert root.get(field), field
        assert root["keywords"] == KEYWORDS
        assert "in vitro toxicology" in KEYWORDS

    def test_the_root_links_to_where_the_profile_actually_resolves(self, doc):
        """`identifier` is the permalink; `url` is the address serving it today."""
        assert _node(doc, PROFILE_IRI)["url"] == PAGES

    def test_the_spec_text_is_cc_by_and_the_root_follows_it(self, doc):
        assert _node(doc, PROFILE_IRI)["license"] == {"@id": CC_BY}

    def test_the_author_is_identified_by_orcid(self, doc):
        assert _node(doc, PROFILE_IRI)["author"] == {"@id": ORCID}
        assert _node(doc, ORCID)["@type"] == "Person"


class TestTheSpecDocument:
    """RO-Crate 1.2: a Profile Crate MUST declare a human-readable description.

    The crate ships the Markdown and names the page GitHub Pages renders from
    it, which is how the published profiles do it — see
    :func:`scripts.gen_profile_crate.build`.
    """

    def test_the_markdown_source_is_a_part_of_the_profile(self, doc):
        assert {"@id": "index.md"} in _node(doc, PROFILE_IRI)["hasPart"]
        spec = _node(doc, "index.md")
        assert spec["encodingFormat"] == "text/markdown"
        assert spec["about"] == {"@id": PROFILE_IRI}
        assert spec["license"] == {"@id": CC_BY}

    def test_the_rendered_page_is_declared_and_resolves_from_anywhere(self, doc):
        """An absolute URI, so the archived crate has nothing dangling.

        Jekyll renders `index.md` server side, so a relative `index.html`
        would name a file no copy of this crate contains.
        """
        page = _node(doc, PAGES)
        assert page["encodingFormat"] == "text/html"
        assert page["about"] == {"@id": PROFILE_IRI}

    def test_a_resource_descriptor_gives_it_the_specification_role(self, doc):
        descriptor = _node(doc, "#hasSpecification")
        assert descriptor["@type"] == "ResourceDescriptor"
        assert descriptor["hasRole"] == {"@id": f"{ROLE}specification"}
        assert descriptor["hasArtifact"] == {"@id": PAGES}
        assert {"@id": "#hasSpecification"} in _node(doc, PROFILE_IRI)["hasResource"]

    def test_the_shipped_markdown_is_the_spec_with_jekyll_front_matter(self, tmp_path):
        build(tmp_path)
        md = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert md.startswith("---\ntitle: ISA-Tox RO-Crate Profile\n---\n"), (
            "the front matter is what titles the rendered page"
        )
        assert "# ISA-Tox RO-Crate Profile" in md
        assert "(figures/isa-tox-model.svg)" in md, "the model figure is not referenced"
        assert (tmp_path / "figures" / "isa-tox-model.svg").exists(), "figure not in the crate"
        assert "mermaid" not in md, "the mermaid summary was replaced by the figure"
        # The requirement tables are the substance of the profile.
        assert md.count("\n|") > 100
        assert not (tmp_path / "index.html").exists(), "the page is Jekyll's to render"


class TestTheShapes:
    def test_every_shape_file_is_in_the_crate_as_mit_turtle(self, doc):
        shipped = {n["@id"] for n in doc["@graph"] if n["@id"].startswith("shapes/")}
        on_disk = {
            f"shapes/{p.name}"
            for p in (REPO / "profiles" / "shapes" / "tox").glob("*.ttl")
        }
        assert shipped == on_disk
        for entity_id in sorted(shipped):
            node = _node(doc, entity_id)
            assert node["encodingFormat"] == "text/turtle"
            assert node["license"] == {"@id": MIT}, entity_id

    def test_the_shapes_have_the_validation_role(self, doc):
        descriptor = _node(doc, "#hasSHACL")
        assert descriptor["hasRole"] == {"@id": f"{ROLE}validation"}
        assert {"@id": "shapes/profile.ttl"} in descriptor["hasArtifact"]

    def test_the_ontology_has_the_vocabulary_role(self, doc):
        descriptor = _node(doc, "#hasVocabulary")
        assert descriptor["hasRole"] == {"@id": f"{ROLE}vocabulary"}
        assert descriptor["hasArtifact"] == {"@id": "shapes/ontology.ttl"}

    def test_the_resources_are_all_declared_on_the_root(self, doc):
        assert _node(doc, PROFILE_IRI)["hasResource"] == [
            {"@id": "#hasSpecification"},
            {"@id": "#hasSHACL"},
            {"@id": "#hasVocabulary"},
        ]

    def test_nothing_disables_jekyll(self, tmp_path):
        """Jekyll is what renders the specification; a .nojekyll would stop it."""
        build(tmp_path)
        assert not (tmp_path / ".nojekyll").exists()
        assert (tmp_path / "shapes" / "profile.ttl").exists()


def test_nothing_generated_is_committed():
    """The crate is generated on demand; a committed copy could go stale."""
    assert not (REPO / "profiles" / "profile_crate").exists()
    assert "dist/" in (REPO / ".gitignore").read_text(encoding="utf-8")
