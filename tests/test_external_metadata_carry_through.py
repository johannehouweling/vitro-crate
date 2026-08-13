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


class TestTheRootIdentifierStaysAString:
    """The two profiles this crate declares contradict each other here.

    RO-Crate 1.2 recommends the root identifier be a PropertyValue entity
    (Science-on-Schema.org); the ISA profile REQUIRES `schema:identifier` on the
    root to have `sh:datatype xsd:string`. A PropertyValue is referenced by IRI,
    so satisfying the recommendation breaks the requirement — and no mixed form
    escapes it, because the SHOULD flags ANY non-PropertyValue identifier while
    the MUST constrains EVERY value of the path.

    A Violation is worse than a Warning, so the string wins and the RO-Crate
    recommendation stays open on purpose. This was tried the other way and
    flipped end-to-end ISA conformance to False.
    """

    def test_it_is_a_plain_string(self):
        state = CrateState()
        state.metadata.accession = "S-VHPS26"
        root = _node(_doc(state), "./")
        assert root is not None
        assert root["identifier"] == "S-VHPS26"

    def test_isa_conformance_survives_it(self):
        """The ISA MUST is `sh:datatype xsd:string`; an IRI reference fails it."""
        from builder.tools.validation import build_and_validate

        state = CrateState()
        state.metadata.title = "An investigation"
        state.metadata.description = "Enough to get past the base requirements."
        state.metadata.accession = "S-VHPS26"
        _add(state, "inv_1", "Investigation", name="An investigation")
        result = build_and_validate(state, severity="required", profile="isa")
        offenders = [
            i
            for i in result["issues"]
            if "identifier" in i["message"] and (i.get("entity_id") or "").endswith("./")
        ]
        assert offenders == [], f"the root identifier broke an ISA MUST: {offenders}"

    def test_isa_identifiers_below_the_root_nest_under_it(self):
        state = CrateState()
        state.metadata.accession = "S-VHPS26"
        _add(state, "inv_1", "Investigation", name="Investigation")
        _add(state, "study_1", "Study", name="A study")
        doc = _doc(state)
        study = next(n for n in doc["@graph"] if "Study" in str(n.get("additionalType")))
        assert isinstance(study["identifier"], str)
        assert study["identifier"].startswith("S-VHPS26/")


class TestTheAgentsOwnAnswerIsNotDeleted:
    """An answer written under the spelling an agent reaches for still counts.

    One session had the agent state what the assay measures
    (`measurement_method`), the build delete it as "not a term in the crate's
    JSON-LD context", and the maturity report then ask for a measurement method.
    The crate reported as missing the very thing it had been told.

    `measurementMethod` is a reference field — normally resolved to a BAO
    DefinedTerm — but the ISA shape takes `sh:or [xsd:string] [schema:DefinedTerm]`,
    so prose is a conformant answer and is kept rather than discarded for want
    of an ontology term.
    """

    def test_a_method_stated_in_prose_reaches_the_assay(self):
        state = CrateState()
        _add(state, "study_1", "Study", name="S")
        _add(
            state,
            "assay_1",
            "Assay",
            name="An assay",
            study_id="study_1",
            measurement_method="T4 uptake; CellTiter-Glo ATP viability control",
        )
        doc = _doc(state)
        assay = next(n for n in doc["@graph"] if "Assay" in str(n.get("additionalType")))
        assert assay["measurementMethod"] == "T4 uptake; CellTiter-Glo ATP viability control"

    def test_the_camel_case_spelling_works_the_same(self):
        state = CrateState()
        _add(state, "study_1", "Study", name="S")
        _add(
            state,
            "assay_1",
            "Assay",
            name="An assay",
            study_id="study_1",
            measurementMethod="Gamma counter",
        )
        doc = _doc(state)
        assay = next(n for n in doc["@graph"] if "Assay" in str(n.get("additionalType")))
        assert assay["measurementMethod"] == "Gamma counter"

    def test_a_broken_reference_is_still_not_emitted(self):
        """A single token that resolves to nothing is a dangling pointer (#180).

        This is the line between the two cases: an entity id and an IRI are
        single tokens, so one that names nothing is a BROKEN reference and must
        not ship as though it were data. A phrase with spaces was never going to
        be an id.
        """
        state = CrateState()
        _add(state, "study_1", "Study", name="S")
        _add(
            state,
            "assay_1",
            "Assay",
            name="An assay",
            study_id="study_1",
            measurementMethod="bao",
        )
        doc = _doc(state)
        assay = next(n for n in doc["@graph"] if "Assay" in str(n.get("additionalType")))
        assert assay.get("measurementMethod") != "bao"

    def test_it_still_resolves_to_a_defined_term_when_one_exists(self):
        """Prose is the fallback, not a replacement for the ontology term."""
        state = CrateState()
        _add(state, "study_1", "Study", name="S")
        _add(state, "term_gamma", "DefinedTerm", name="Gamma counter", termCode="BAO:0000110")
        _add(
            state,
            "assay_1",
            "Assay",
            name="An assay",
            study_id="study_1",
            measurement_method="term_gamma",
        )
        doc = _doc(state)
        assay = next(n for n in doc["@graph"] if "Assay" in str(n.get("additionalType")))
        assert isinstance(assay["measurementMethod"], dict), "an in-crate term must resolve"


class TestContactDetailsBecomeAnEntity:
    """A contact the human gives has to become something the shapes can point at.

    Both profiles want an entity, not a literal: an Organization's
    ``contactPoint`` SHOULD reference a ContactPoint, and the root's authors or
    publishers SHOULD have one between them. An email written as a string on the
    Person satisfies neither — the same reference-not-literal rule that already
    governs affiliation and creator.

    Nothing here is invented. A crate with no contact details emits no
    ContactPoint and keeps the finding open, because the only legitimate source
    for a real person's address is the human.
    """

    def test_an_email_becomes_a_referenced_contact_point(self):
        state = CrateState()
        _add(state, "org_uu", "Organization", name="Utrecht University", email="info@uu.nl")
        doc = _doc(state)
        org = next(n for n in doc["@graph"] if n.get("@type") == "Organization")
        ref = org["contactPoint"]
        contact = _node(doc, ref[0]["@id"] if isinstance(ref, list) else ref["@id"])
        assert contact is not None
        assert contact["@type"] == "ContactPoint"
        assert contact["email"] == "info@uu.nl"

    def test_the_literal_does_not_also_ship(self):
        """Two spellings of the same fact invite them to disagree later."""
        state = CrateState()
        _add(state, "org_uu", "Organization", name="Utrecht University", email="info@uu.nl")
        org = next(n for n in _doc(state)["@graph"] if n.get("@type") == "Organization")
        assert "email" not in org

    def test_a_person_gets_one_too(self):
        state = CrateState()
        _add(state, "p1", "Person", name="Ada Lovelace", email="ada@example.org")
        doc = _doc(state)
        person = next(n for n in doc["@graph"] if n.get("@type") == "Person")
        assert "contactPoint" in person

    def test_a_mailto_prefix_is_stripped(self):
        """It is how a human writes it; schema.org wants the bare address."""
        state = CrateState()
        _add(state, "p1", "Person", name="Ada", email="mailto:ada@example.org")
        doc = _doc(state)
        contact = next(n for n in doc["@graph"] if n.get("@type") == "ContactPoint")
        assert contact["email"] == "ada@example.org"

    def test_the_answer_to_a_contact_point_gap_lands(self):
        """Guidance commits the gap's own field name, so it must work too."""
        state = CrateState()
        _add(state, "org_uu", "Organization", name="Utrecht", contactPoint="info@uu.nl")
        doc = _doc(state)
        contact = next(n for n in doc["@graph"] if n.get("@type") == "ContactPoint")
        assert contact["email"] == "info@uu.nl"

    def test_a_telephone_alone_is_enough(self):
        state = CrateState()
        _add(state, "org_uu", "Organization", name="Utrecht", telephone="+31 30 253 5000")
        doc = _doc(state)
        contact = next(n for n in doc["@graph"] if n.get("@type") == "ContactPoint")
        assert contact["telephone"] == "+31 30 253 5000"
        assert "email" not in contact

    def test_no_contact_details_invent_nothing(self):
        state = CrateState()
        _add(state, "org_uu", "Organization", name="Utrecht University")
        doc = _doc(state)
        assert not [n for n in doc["@graph"] if n.get("@type") == "ContactPoint"]
        org = next(n for n in doc["@graph"] if n.get("@type") == "Organization")
        assert "contactPoint" not in org

    def test_one_address_is_one_entity(self):
        """Two people on the same lab address must not mint two ContactPoints."""
        state = CrateState()
        _add(state, "p1", "Person", name="Ada", email="lab@example.org")
        _add(state, "p2", "Person", name="Grace", email="lab@example.org")
        doc = _doc(state)
        contacts = [n for n in doc["@graph"] if n.get("@type") == "ContactPoint"]
        assert len(contacts) == 1


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
