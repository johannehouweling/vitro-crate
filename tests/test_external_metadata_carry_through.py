"""Metadata we fetch or set reaches the crate under the predicate it belongs to.

Four findings arrived from one session, each reading as "the crate is missing X".
None of them was a retrieval failure — every one had the data in hand:

* An article whose seven authors all resolved to Person nodes was reported as
  having no author, because our own ``@context`` aliased ``author`` to
  ``schema:creator``. The block is LAST in the crate's ``@context``, so it
  overrode RO-Crate's correct mapping and silently rewrote every author in the
  graph. The shape asked for ``schema:author`` and found nothing.
* Organizations carried a ROR IRI but no website, though ROR states the website
  on the record that IRI names.
* The licence was a bare URL string, so the License entity had no name and no
  description to have.
* The root identifier was a plain string where the profile asks for a
  PropertyValue naming its scheme.

The first is the one that matters most: it was invisible. A misfiled predicate
does not look like a bug from inside the crate — the value is right there in the
JSON — and it took reading the RDF to see it. These tests assert on the expanded
graph rather than the JSON so that a future alias cannot hide the same way.
"""

from __future__ import annotations

import json

import pytest
from rdflib import Graph, URIRef
from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools._crate_mapping import populate_crate
from builder.tools.composites import _find_or_draft_organization
from profiles.context import ISA_TOX_CONTEXT
from profiles.licenses import describe_license

SCHEMA = "http://schema.org/"


def _doc(state: CrateState) -> dict:
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, None, materialize_payload=False, include_all_scanned=False)
    return crate.metadata.generate()


def _rdf(state: CrateState) -> Graph:
    """The crate as triples — where a misfiled predicate becomes visible."""
    g = Graph()
    g.parse(data=json.dumps(_doc(state)), format="json-ld")
    return g


def _node(doc: dict, node_id: str) -> dict | None:
    return next((n for n in doc["@graph"] if n.get("@id") == node_id), None)


def _add(state: CrateState, entity_id: str, entity_type: EntityType, **fields) -> Entity:
    e = Entity(
        entity_id=entity_id, type=entity_type, _provenance=EntityProvenance(created_by="lookup")
    )
    e.set_fields_from_dict(fields, source="lookup")
    state.add_entity(e)
    return e


@pytest.fixture
def state_with_article() -> CrateState:
    state = CrateState()
    _add(state, "https://orcid.org/0000-0002-5733-3290", "Person", name="Timo Hamers")
    _add(
        state,
        "pub_thyroid_transporters",
        "Publication",
        name="Two novel in vitro assays",
        url="https://doi.org/10.1007/s00204-024-03787-2",
        identifier="https://doi.org/10.1007/s00204-024-03787-2",
        author=["https://orcid.org/0000-0002-5733-3290"],
    )
    return state


class TestAuthorIsNotCreator:
    """`schema:author` and `schema:creator` are different properties."""

    def test_the_context_does_not_alias_author_to_creator(self):
        blocks = ISA_TOX_CONTEXT if isinstance(ISA_TOX_CONTEXT, list) else [ISA_TOX_CONTEXT]
        mapped = [b["author"] for b in blocks if isinstance(b, dict) and "author" in b]
        assert mapped, "the context is expected to define `author`"
        for target in mapped:
            assert target == f"{SCHEMA}author", f"author must not be aliased to {target}"

    def test_an_article_author_expands_to_schema_author(self, state_with_article):
        article = URIRef("https://doi.org/10.1007/s00204-024-03787-2")
        authors = set(_rdf(state_with_article).objects(article, URIRef(f"{SCHEMA}author")))
        assert URIRef("https://orcid.org/0000-0002-5733-3290") in authors

    def test_the_author_does_not_land_on_creator(self, state_with_article):
        """The regression itself: authors arriving as creators, and no author at all."""
        article = URIRef("https://doi.org/10.1007/s00204-024-03787-2")
        creators = set(_rdf(state_with_article).objects(article, URIRef(f"{SCHEMA}creator")))
        assert URIRef("https://orcid.org/0000-0002-5733-3290") not in creators

    def test_a_real_creator_still_expands_to_creator(self, state_with_article):
        """Fixing the alias must not cost the property it was aliased to."""
        state_with_article.metadata.title = "An investigation"
        _add(state_with_article, "inv_1", "Investigation", name="An investigation")
        assert ISA_TOX_CONTEXT
        blocks = ISA_TOX_CONTEXT if isinstance(ISA_TOX_CONTEXT, list) else [ISA_TOX_CONTEXT]
        creator = [b["creator"] for b in blocks if isinstance(b, dict) and "creator" in b]
        assert creator == [f"{SCHEMA}creator"]


class TestTheLicenceSaysWhatItIs:
    def test_a_known_licence_becomes_a_described_entity(self):
        state = CrateState()
        state.metadata.license = "https://creativecommons.org/licenses/by/4.0/"
        doc = _doc(state)
        root = _node(doc, "./")
        assert root is not None
        assert root["license"] == {"@id": "https://creativecommons.org/licenses/by/4.0/"}
        entity = _node(doc, "https://creativecommons.org/licenses/by/4.0/")
        assert entity is not None
        assert entity["name"] == "Creative Commons Attribution 4.0 International"
        assert entity["description"]

    def test_an_unrecognised_licence_is_left_exactly_as_given(self):
        """Better a bare URL than a licence we named wrongly."""
        state = CrateState()
        state.metadata.license = "https://example.org/lab-terms"
        root = _node(_doc(state), "./")
        assert root is not None
        assert root["license"] == "https://example.org/lab-terms"

    def test_the_placeholder_stays_a_string(self):
        root = _node(_doc(CrateState()), "./")
        assert root is not None
        assert root["license"] == "ALL RIGHTS RESERVED BY THE AUTHORS"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://creativecommons.org/licenses/by/4.0/",
                "Creative Commons Attribution 4.0 International",
            ),
            (
                "https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode",
                "Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International",
            ),
            (
                "https://creativecommons.org/licenses/by-sa/3.0/",
                "Creative Commons Attribution-ShareAlike 3.0 International",
            ),
            (
                "https://creativecommons.org/publicdomain/zero/1.0/",
                "CC0 1.0 Universal Public Domain Dedication",
            ),
            ("https://opensource.org/licenses/MIT", "MIT License"),
        ],
    )
    def test_the_family_is_derived_not_tabulated(self, url, expected):
        """Combinations we have never shipped are named correctly too."""
        described = describe_license(url)
        assert described is not None
        assert described["name"] == expected

    @pytest.mark.parametrize(
        "value",
        ["", "ALL RIGHTS RESERVED BY THE AUTHORS", "CC-BY-4.0", "https://example.org/x"],
    )
    def test_what_it_cannot_name_it_declines_to_name(self, value):
        assert describe_license(value) is None


class TestTheRootIdentifierNamesItsScheme:
    def test_it_is_a_property_value_entity(self):
        state = CrateState()
        state.metadata.accession = "S-VHPS26"
        doc = _doc(state)
        root = _node(doc, "./")
        assert root is not None
        ref = root["identifier"]
        assert isinstance(ref, dict), "the profile asks for a PropertyValue, not a string"
        node = _node(doc, ref["@id"])
        assert node is not None
        assert node["@type"] == "PropertyValue"
        assert node["value"] == "S-VHPS26"

    def test_a_doi_accession_carries_its_resolver(self):
        state = CrateState()
        state.metadata.accession = "https://doi.org/10.1007/s00204-024-03787-2"
        doc = _doc(state)
        root = _node(doc, "./")
        assert root is not None
        node = _node(doc, root["identifier"]["@id"])
        assert node is not None
        assert node["propertyID"] == {"@id": "https://registry.identifiers.org/registry/doi"}

    def test_an_opaque_accession_claims_no_scheme(self):
        """D5: an internal slug belongs to no registry, so none is named."""
        state = CrateState()
        state.metadata.accession = "inv_local_slug"
        doc = _doc(state)
        root = _node(doc, "./")
        assert root is not None
        node = _node(doc, root["identifier"]["@id"])
        assert node is not None
        assert "propertyID" not in node

    def test_isa_identifiers_below_the_root_stay_strings(self):
        """They nest textually under the root's — an entity there breaks them."""
        state = CrateState()
        state.metadata.accession = "S-VHPS26"
        _add(state, "inv_1", "Investigation", name="Investigation")
        _add(state, "study_1", "Study", name="A study")
        doc = _doc(state)
        study = next(n for n in doc["@graph"] if "Study" in str(n.get("additionalType")))
        assert isinstance(study["identifier"], str)
        assert study["identifier"].startswith("S-VHPS26/")

    def test_no_identifier_means_no_node(self):
        doc = _doc(CrateState())
        root = _node(doc, "./")
        assert root is not None
        assert "identifier" not in root or root["identifier"] == ""


class TestOrganizationsCarryTheirWebsite:
    """ROR states the website on the record; dropping it costs a finding."""

    def test_a_known_ror_brings_the_url(self, monkeypatch):
        monkeypatch.setattr(
            "builder.tools.composites.fetch_ror_by_id",
            lambda rid: {"url": "https://www.uu.nl"},
        )
        state = CrateState()
        org_id = _find_or_draft_organization(
            state, "Utrecht University", "https://ror.org/04pp8hn57"
        )
        assert org_id is not None
        org = state.get_entity(org_id)
        assert org is not None
        assert org.fields["url"] == "https://www.uu.nl"

    def test_a_late_ror_backfills_the_url(self, monkeypatch):
        """Two authors share an employer; only the second call carries the ROR."""
        monkeypatch.setattr(
            "builder.tools.composites.fetch_ror_by_id",
            lambda rid: {"url": "https://vu.nl/"},
        )
        state = CrateState()
        first = _find_or_draft_organization(state, "Vrije Universiteit Amsterdam", None)
        second = _find_or_draft_organization(
            state, "Vrije Universiteit Amsterdam", "https://ror.org/008xxew50"
        )
        assert first == second, "the same employer must not become two organizations"
        assert second is not None
        org = state.get_entity(second)
        assert org is not None
        assert org.fields["url"] == "https://vu.nl/"
        assert org.fields["ror"] == "https://ror.org/008xxew50"

    def test_the_url_reaches_the_crate(self):
        state = CrateState()
        _add(state, "org_uu", "Organization", name="Utrecht University", url="https://www.uu.nl")
        node = next(n for n in _doc(state)["@graph"] if n.get("@type") == "Organization")
        assert node["url"] == "https://www.uu.nl"

    def test_a_ror_outage_never_breaks_the_cascade(self, monkeypatch):
        """A missing website is a recommendation; a failed build is not."""

        def boom(rid):
            raise RuntimeError("ROR is down")

        monkeypatch.setattr("builder.tools.composites.fetch_ror_by_id", boom)
        state = CrateState()
        org_id = _find_or_draft_organization(
            state, "Utrecht University", "https://ror.org/04pp8hn57"
        )
        assert org_id is not None
        org = state.get_entity(org_id)
        assert org is not None
        assert "url" not in org.fields
        assert org.fields["ror"] == "https://ror.org/04pp8hn57"

    def test_no_ror_means_no_fetch(self, monkeypatch):
        """A name alone is a guess; only an established id is dereferenced."""
        calls = []
        monkeypatch.setattr(
            "builder.tools.composites.fetch_ror_by_id",
            lambda rid: calls.append(rid) or {"url": "https://example.org"},
        )
        state = CrateState()
        _find_or_draft_organization(state, "Some Unregistered Lab", None)
        assert calls == []
