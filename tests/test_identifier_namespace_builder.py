"""What the build writes is what the identifier-namespace shapes accept.

The shapes check an identifier's form against the Bioregistry's pattern. The
build must not be the cause of a finding: an ORCID, ROR ID, Cellosaurus accession
or DOI a deposit states as `http://…`, with a trailing slash or on a legacy host is
written as the one address the shape expects. Anything that is not an identifier
of that registry is left as it was given, so the shape reports it.

A reference that holds the identifier in the form it was stated in still reaches
the node, and two entities whose identifiers are one address are written as one
node rather than one replacing the other. An Organization drafted under another
spelling of a name the crate holds amends that one instead of overwriting it.

A REQUIRED finding about an identifier is never set aside as one about cited
vocabulary: it is what fails the layer.
"""

from __future__ import annotations

import re

import pytest

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools._crate_mapping import _mint_id
from builder.tools.builder import assemble_crate
from builder.tools.drafters import draft_organization, draft_property_value
from builder.tools.validation import _partition_citations
from profiles.identifier_namespaces import iri_pattern


def _ent(entity_id: str, type_: EntityType, **fields) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="lookup"),
    )


# (entity type, field, prefix, stated forms, the one address each is written as)
_STATED = [
    (
        "Person",
        "orcid",
        "orcid",
        [
            "0000-0002-1825-009X",
            "0000-0002-1825-009x",
            "https://orcid.org/0000-0002-1825-009X",
            "http://orcid.org/0000-0002-1825-009X",
            "https://orcid.org/0000-0002-1825-009X/",
        ],
        "https://orcid.org/0000-0002-1825-009X",
    ),
    (
        "Organization",
        "ror",
        "ror",
        ["05gq02987", "https://ror.org/05gq02987", "http://ror.org/05GQ02987", "https://ror.org/05gq02987/"],
        "https://ror.org/05gq02987",
    ),
    (
        "CellLineSample",
        "accession",
        "cellosaurus",
        [
            "CVCL_0265",
            "RRID:CVCL_0265",
            "cvcl_0265",
            "https://www.cellosaurus.org/CVCL_0265",
            "https://web.expasy.org/cellosaurus/CVCL_0265",
        ],
        "https://www.cellosaurus.org/CVCL_0265",
    ),
    (
        "Publication",
        "doi",
        "doi",
        [
            "10.1016/j.tiv.2023.105770",
            "doi:10.1016/j.tiv.2023.105770",
            "https://doi.org/10.1016/j.tiv.2023.105770",
            "http://doi.org/10.1016/j.tiv.2023.105770",
            "https://dx.doi.org/10.1016/j.tiv.2023.105770",
        ],
        "https://doi.org/10.1016/j.tiv.2023.105770",
    ),
]


class TestTheBuildWritesTheRegistrysAddress:
    @pytest.mark.parametrize(("type_", "field", "prefix", "forms", "address"), _STATED, ids=lambda v: str(v)[:20])
    def test_every_stated_form_becomes_the_one_address(self, type_, field, prefix, forms, address):
        for form in forms:
            minted = _mint_id(_ent("x", type_, **{field: form}))
            assert minted == address, (form, minted)
            assert re.search(iri_pattern(prefix), minted), minted

    def test_what_is_not_an_identifier_of_the_registry_is_left_alone(self):
        # Kept as given, so the shape reports it rather than the build hiding it.
        assert _mint_id(_ent("x", "Person", orcid="https://example.org/people/1")) == (
            "https://example.org/people/1"
        )
        assert _mint_id(_ent("x", "Organization", ror="Utrecht University")) == (
            "https://ror.org/Utrecht University"
        )


class TestADoiValueIsBare:
    @pytest.mark.parametrize(
        "stated",
        ["10.1234/paper", "https://doi.org/10.1234/paper", "doi:10.1234/paper", "https://dx.doi.org/10.1234/paper"],
    )
    def test_the_value_is_the_doi_alone(self, stated):
        state = CrateState()
        pv = draft_property_value(state, "DOI", {"value": stated})
        assert pv.fields["value"] == "10.1234/paper"

    def test_another_property_value_keeps_its_value(self):
        state = CrateState()
        pv = draft_property_value(state, "Passage Number", {"value": "https://doi.org/x"})
        assert pv.fields["value"] == "https://doi.org/x"


class TestARequiredFindingIsNeverSetAside:
    def test_a_required_finding_on_a_cited_iri_stays_a_gap(self):
        cited = {"http://purl.obolibrary.org/obo/NCIT_16403"}
        issues = [
            {"entity_id": "http://purl.obolibrary.org/obo/NCIT_16403", "severity": "required"},
            {"entity_id": "http://purl.obolibrary.org/obo/NCIT_16403", "severity": "recommended"},
        ]
        gaps, citations = _partition_citations(issues, cited)
        assert [i["severity"] for i in gaps] == ["required"]
        assert [i["severity"] for i in citations] == ["recommended"]


def _graph(state: CrateState) -> dict[str, dict]:
    crate = assemble_crate(state, materialize_payload=False, include_all_scanned=False)
    return {n["@id"]: n for n in crate.metadata.generate()["@graph"]}


def _ids(value) -> list[str]:
    return [v["@id"] for v in (value if isinstance(value, list) else [value]) if isinstance(v, dict)]


class TestAStatedFormStillReachesTheNode:
    def test_a_reference_in_the_stated_form_is_the_written_node(self):
        state = CrateState()
        state.add_entity(_ent("p1", "Person", name="Ada Lovelace", orcid="http://orcid.org/0000-0002-1825-009X"))
        state.add_entity(
            _ent(
                "pub1",
                "Publication",
                name="Notes",
                doi="10.1234/notes",
                author=["http://orcid.org/0000-0002-1825-009X"],
            )
        )
        graph = _graph(state)
        assert "https://orcid.org/0000-0002-1825-009X" in graph
        assert _ids(graph["https://doi.org/10.1234/notes"]["author"]) == [
            "https://orcid.org/0000-0002-1825-009X"
        ]

    def test_a_bare_identifier_reference_reaches_the_node(self):
        state = CrateState()
        state.add_entity(_ent("o1", "Organization", name="Utrecht University", ror="04pp8hn57"))
        state.add_entity(_ent("p1", "Person", name="Ada Lovelace", affiliation="04pp8hn57"))
        graph = _graph(state)
        person = next(n for n in graph.values() if n.get("name") == "Ada Lovelace")
        assert _ids(person["affiliation"]) == ["https://ror.org/04pp8hn57"]


class TestOneIdentifierIsOneNode:
    def test_two_people_with_one_orcid_are_written_as_one(self):
        state = CrateState()
        state.add_entity(_ent("p1", "Person", name="Ada Lovelace", orcid="0000-0002-1825-009X", email="ada@example.org"))
        state.add_entity(_ent("p2", "Person", name="A. Lovelace", orcid="https://orcid.org/0000-0002-1825-009x"))
        graph = _graph(state)
        people = [n for n in graph.values() if n.get("@type") == "Person"]
        assert len(people) == 1
        (person,) = people
        assert person["name"] == "Ada Lovelace"
        assert person["alternateName"] == "A. Lovelace"

    def test_two_organisations_with_one_ror_id_are_written_as_one(self):
        state = CrateState()
        state.add_entity(_ent("o1", "Organization", name="Utrecht University", ror="04pp8hn57"))
        state.add_entity(_ent("o2", "Organization", name="Universiteit Utrecht", ror="https://ror.org/04pp8hn57"))
        graph = _graph(state)
        orgs = [n for n in graph.values() if n.get("@type") == "Organization"]
        assert [o["@id"] for o in orgs] == ["https://ror.org/04pp8hn57"]
        assert orgs[0]["alternateName"] == "Universiteit Utrecht"

    def test_the_orcid_value_is_spelled_as_the_address_is(self):
        state = CrateState()
        state.add_entity(_ent("p1", "Person", name="Ada Lovelace", orcid="http://orcid.org/0000-0002-1825-009x"))
        graph = _graph(state)
        values = [n["value"] for n in graph.values() if n.get("name") == "ORCID" and "value" in n]
        assert values == ["0000-0002-1825-009X"]


class TestAnotherSpellingAmendsTheOrganisation:
    def test_a_case_variant_keeps_what_the_organisation_states(self):
        state = CrateState()
        state.add_entity(
            _ent("org_utrecht_university", "Organization", name="Utrecht University", url="https://www.uu.nl")
        )
        org = draft_organization(state, "UTRECHT UNIVERSITY", {})
        assert org.entity_id == "org_utrecht_university"
        assert org.fields["name"] == "Utrecht University"
        assert org.fields["url"] == "https://www.uu.nl"
        assert len(state.list_entities("Organization")) == 1

    def test_a_redraft_without_a_ror_id_keeps_the_one_held(self):
        state = CrateState()
        state.add_entity(
            _ent("org_utrecht_university", "Organization", name="Utrecht University", ror="04pp8hn57")
        )
        org = draft_organization(state, "Utrecht University", {})
        assert org.fields["ror"] == "04pp8hn57"

    def test_two_ror_ids_are_two_organisations(self):
        state = CrateState()
        state.add_entity(
            _ent("org_utrecht_university", "Organization", name="Utrecht University", ror="04pp8hn57")
        )
        other = draft_organization(state, "UTRECHT UNIVERSITY", {"ror": "0575yy874"})
        assert other.entity_id != "org_utrecht_university"
        assert {o.fields.get("ror") for o in state.list_entities("Organization")} == {
            "04pp8hn57",
            "0575yy874",
        }
