"""A chemical is identified by its Compound Wiki address.

Where a lookup found the chemical in the VHP4Safety Compound Wiki, the item's
address, ``https://compoundcloud.wikibase.cloud/entity/Q<n>``, is the
``MolecularEntity``'s ``@id``. A chemical the wiki has no item for keeps its
PubChem address, then its ChEBI one, then a local id, as before.

Only an id a lookup wrote counts (D5). ``resolve_compound`` records the id it
finds as looked up; one a caller or a model handed in is recorded as theirs, is
still written as an identifier value, and never becomes the ``@id``. The
identifier values (CAS, PubChem CID, DTXSID, Compound Wiki) are the same
whichever address the chemical sits at.

Every assertion reads the assembled JSON-LD graph: no network and no SHACL.
"""

from __future__ import annotations

from typing import Any

import pytest
from rocrate.rocrate import ROCrate

from builder.state import CompletionSource, CrateState, Entity, EntityProvenance, EntityType
from builder.tools._crate_mapping import _mint_id, populate_crate
from profiles.context import ISA_TOX_CONTEXT

WIKI = "https://compoundcloud.wikibase.cloud/entity/"
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/compound/"
CHEBI = "http://purl.obolibrary.org/obo/CHEBI_27732"

# Caffeine as PubChem, CompTox and the Compound Wiki state it.
CAFFEINE: dict[str, Any] = {"cas": "58-08-2", "pubchem_cid": "2519", "dtxsid": "DTXSID0020232"}


def _ent(entity_id: str, type_: EntityType, **fields) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _chemical(
    entity_id: str,
    name: str,
    *,
    wiki_id: str | None = None,
    written_by: CompletionSource = "lookup",
    **fields,
) -> Entity:
    """A chemical whose Compound Wiki id, when it has one, *written_by* wrote."""
    chem = _ent(entity_id, "MolecularEntity", name=name, **fields)
    if wiki_id is not None:
        chem.set_fields_from_dict({"compoundwiki_id": wiki_id}, source=written_by)
    return chem


def _state(*entities: Entity) -> CrateState:
    state = CrateState()
    state.metadata.title = "Compound crate"
    for entity in entities:
        state.add_entity(entity)
    return state


def _graph(state: CrateState) -> list[dict]:
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, None, materialize_payload=False, include_all_scanned=False)
    return crate.metadata.generate()["@graph"]


def _by_id(graph: list[dict], node_id: str) -> dict | None:
    return next((n for n in graph if n.get("@id") == node_id), None)


def _ids(value: object) -> list[str]:
    if value is None:
        return []
    return [
        v["@id"] if isinstance(v, dict) else v
        for v in (value if isinstance(value, list) else [value])
    ]


def _chemicals(graph: list[dict]) -> list[dict]:
    return [n for n in graph if n.get("@type") == "MolecularEntity"]


def _identifiers(graph: list[dict], node: dict) -> list[tuple[str, str]]:
    """The identifier values of *node* as (scheme, value), in the order written."""
    values = [_by_id(graph, ref) for ref in _ids(node.get("identifier"))]
    return [(pv["name"], pv["value"]) for pv in values if pv is not None]


class TestALookedUpCompoundWikiIdIsTheAddress:
    def test_the_address_is_the_wiki_item(self):
        chem = _chemical("chem_caffeine", "Caffeine", wiki_id="Q2026", **CAFFEINE)
        assert _mint_id(chem) == WIKI + "Q2026"

    def test_the_node_sits_at_that_address(self):
        graph = _graph(_state(_chemical("chem_caffeine", "Caffeine", wiki_id="Q2026", **CAFFEINE)))
        (node,) = _chemicals(graph)
        assert node["@id"] == WIKI + "Q2026"
        assert node["name"] == "Caffeine"
        assert _by_id(graph, PUBCHEM + "2519") is None

    def test_it_comes_before_the_chebi_address(self):
        chem = _chemical("chem_caffeine", "Caffeine", wiki_id="Q2026", sameAs={"@id": CHEBI})
        assert _mint_id(chem) == WIKI + "Q2026"

    def test_it_stands_where_no_other_address_exists(self):
        chem = _chemical("chem_caffeine", "Caffeine", wiki_id="Q2026")
        assert _mint_id(chem) == WIKI + "Q2026"

    def test_the_identifier_values_are_the_ones_written_at_the_pubchem_address(self):
        """The address changes; CAS, PubChem CID, DTXSID and Compound Wiki do not."""
        looked_up = _graph(
            _state(_chemical("chem_caffeine", "Caffeine", wiki_id="Q2026", **CAFFEINE))
        )
        typed = _graph(
            _state(
                _chemical(
                    "chem_caffeine", "Caffeine", wiki_id="Q2026", written_by="llm", **CAFFEINE
                )
            )
        )
        at_wiki, at_pubchem = _by_id(looked_up, WIKI + "Q2026"), _by_id(typed, PUBCHEM + "2519")
        assert at_wiki is not None and at_pubchem is not None
        assert _identifiers(looked_up, at_wiki) == [
            ("CAS", "58-08-2"),
            ("PubChem CID", "2519"),
            ("DTXSID", "DTXSID0020232"),
            ("Compound Wiki", "Q2026"),
        ]
        assert at_wiki["identifier"] == at_pubchem["identifier"]
        for ref in _ids(at_wiki["identifier"]):
            assert _by_id(looked_up, ref) == _by_id(typed, ref)


class TestOnlyALookupNamesTheAddress:
    """D5: an id a caller or a model handed in is an identifier value, never the @id."""

    @pytest.mark.parametrize("written_by", ["llm", "user", "scanner"])
    def test_an_id_no_lookup_wrote_leaves_the_pubchem_address(self, written_by):
        chem = _chemical(
            "chem_caffeine", "Caffeine", wiki_id="Q2026", written_by=written_by, **CAFFEINE
        )
        assert _mint_id(chem) == PUBCHEM + "2519"

    def test_an_id_with_no_record_of_who_wrote_it_leaves_the_pubchem_address(self):
        chem = _chemical("chem_caffeine", "Caffeine", compoundwiki_id="Q2026", **CAFFEINE)
        assert _mint_id(chem) == PUBCHEM + "2519"

    def test_a_typed_id_alone_leaves_the_local_id(self):
        chem = _chemical("chem_caffeine", "Caffeine", wiki_id="Q2026", written_by="llm")
        assert _mint_id(chem) == "#MolecularEntity_chem_caffeine"

    def test_a_typed_id_is_still_written_as_an_identifier(self):
        graph = _graph(
            _state(
                _chemical(
                    "chem_caffeine", "Caffeine", wiki_id="Q2026", written_by="llm", **CAFFEINE
                )
            )
        )
        (node,) = _chemicals(graph)
        assert node["@id"] == PUBCHEM + "2519"
        assert _identifiers(graph, node)[-1] == ("Compound Wiki", "Q2026")

    def test_an_id_a_person_types_over_the_looked_up_one_is_not_the_address(self):
        chem = _chemical("chem_caffeine", "Caffeine", wiki_id="Q2026", **CAFFEINE)
        chem.set_fields_from_dict({"compoundwiki_id": "Q1881"}, source="user")
        assert _mint_id(chem) == PUBCHEM + "2519"

    @pytest.mark.parametrize(
        "value",
        ["Item:Q2026", "q2026", WIKI + "Q2026", "Q2026/../Q1", " Q2026", "Q", "2026", ""],
    )
    def test_a_value_that_is_no_item_id_is_never_joined_into_an_address(self, value):
        chem = _chemical("chem_caffeine", "Caffeine", wiki_id=value, **CAFFEINE)
        assert _mint_id(chem) == PUBCHEM + "2519"


class TestAChemicalTheWikiHasNoItemFor:
    """Without a looked-up Compound Wiki id the order is the one it was."""

    def test_the_pubchem_address_stays(self):
        assert _mint_id(_chemical("chem_caffeine", "Caffeine", **CAFFEINE)) == PUBCHEM + "2519"

    def test_then_the_chebi_address(self):
        chem = _chemical("chem_caffeine", "Caffeine", sameAs={"@id": CHEBI})
        assert _mint_id(chem) == CHEBI

    def test_then_the_local_id(self):
        assert _mint_id(_chemical("chem_x", "Test item X")) == "#MolecularEntity_chem_x"


class TestAChemicalIsNotTheSameAsItself:
    """The by-name record of the wiki states its item as ``sameAs``."""

    def test_the_link_is_left_out_when_the_item_is_the_address(self):
        chem = _chemical(
            "chem_caffeine", "Caffeine", wiki_id="Q2026", sameAs={"@id": WIKI + "Q2026"}
        )
        (node,) = _chemicals(_graph(_state(chem)))
        assert node["@id"] == WIKI + "Q2026"
        assert "sameAs" not in node

    def test_the_link_stays_when_the_item_was_not_found_by_inchikey(self):
        chem = _chemical("chem_caffeine", "Caffeine", sameAs={"@id": WIKI + "Q2026"}, **CAFFEINE)
        (node,) = _chemicals(_graph(_state(chem)))
        assert node["@id"] == PUBCHEM + "2519"
        assert node["sameAs"] == {"@id": WIKI + "Q2026"}

    def test_a_link_to_another_record_stays(self):
        chem = _chemical("chem_caffeine", "Caffeine", wiki_id="Q2026", sameAs={"@id": CHEBI})
        (node,) = _chemicals(_graph(_state(chem)))
        assert node["@id"] == WIKI + "Q2026"
        assert node["sameAs"] == {"@id": CHEBI}
