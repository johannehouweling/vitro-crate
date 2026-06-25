"""Looked-up identifiers round-trip into the crate as PropertyValue nodes (#180).

The deterministic build path (`populate_crate`) turns identifier-bearing fields
on Person / MolecularEntity into `schema:PropertyValue` identifier nodes whose
shapes match the gold crate
(`crates_out/S-VHPS21_rocrate/ro-crate-metadata.json`):

* Person.orcid -> a single ORCID PropertyValue ref, propertyID an `@id` IRI node.
* MolecularEntity cas + pubchem_cid -> `[CAS, PubChem CID]` PropertyValue refs.
* Person.affiliation -> an `{@id}` reference to its Organization (ROR), not a literal.
* ScholarlyArticle.author -> an array of Person references.

Entity ids mirror rocrate-wizard's `param_id` scheme
(`#param_<slug(name)>_<sha1("name|value")[:10]>`) so the output matches the gold
crate byte-for-byte at the id level. All assertions read the assembled JSON-LD
graph; no network and no SHACL (no `build_and_validate`) so the module is fast.
"""

from __future__ import annotations

from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import populate_crate
from profiles.context import ISA_TOX_CONTEXT


def _graph(state: CrateState) -> list[dict]:
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, None, materialize_payload=False, include_all_scanned=False)
    return crate.metadata.generate()["@graph"]


def _by_id(graph: list[dict], node_id: str) -> dict | None:
    return next((n for n in graph if n.get("@id") == node_id), None)


def _node_of_type(graph: list[dict], type_name: str) -> dict | None:
    for n in graph:
        t = n.get("@type")
        if t == type_name or (isinstance(t, list) and type_name in t):
            return n
    return None


def _ref_ids(value: object) -> list[str]:
    """The list of @id strings referenced by an identifier/author property value."""
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for it in items:
        if isinstance(it, dict):
            ref = it.get("@id")
            if isinstance(ref, str):
                out.append(ref)
    return out


def _person_state() -> CrateState:
    state = CrateState()
    state.metadata.title = "Person crate"
    person = Entity(
        entity_id="person_wagenaars",
        type="Person",
        fields={"name": "F.M.A. Wagenaars", "orcid": "0000-0003-4766-7358"},
        _provenance=EntityProvenance(created_by="llm"),
    )
    state.add_entity(person)
    return state


class TestPersonOrcidIdentifier:
    def test_person_carries_orcid_property_value_ref(self):
        graph = _graph(_person_state())
        person = _by_id(graph, "https://orcid.org/0000-0003-4766-7358")
        assert person is not None, "Person @id must be its ORCID URL"
        ids = _ref_ids(person.get("identifier"))
        assert ids == ["#param_ORCID_241def7f8f"], (
            f"Person.identifier must reference the ORCID PropertyValue, got {ids}"
        )

    def test_orcid_property_value_node_matches_gold(self):
        graph = _graph(_person_state())
        pv = _by_id(graph, "#param_ORCID_241def7f8f")
        assert pv is not None, "ORCID PropertyValue node must be in the graph"
        assert pv.get("@type") == "PropertyValue"
        assert pv.get("name") == "ORCID"
        assert pv.get("value") == "0000-0003-4766-7358"
        # propertyID MUST be an @id IRI node, not a string literal.
        assert pv.get("propertyID") == {"@id": "https://orcid.org"}

    def test_person_without_orcid_has_no_identifier(self):
        state = CrateState()
        state.add_entity(
            Entity(
                entity_id="p_noid",
                type="Person",
                fields={"name": "Jane Doe"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        graph = _graph(state)
        person = _node_of_type(graph, "Person")
        assert person is not None
        assert "identifier" not in person, "No fabricated identifier without an ORCID (D5)"


def _molecular_state() -> CrateState:
    state = CrateState()
    state.metadata.title = "Compound crate"
    chem = Entity(
        entity_id="chem_silychristin",
        type="MolecularEntity",
        fields={
            "name": "Silychristin A",
            "cas": "33889-69-9",
            "pubchem_cid": "441764",
        },
        _provenance=EntityProvenance(created_by="llm"),
    )
    state.add_entity(chem)
    return state


class TestMolecularEntityIdentifiers:
    def test_entity_id_is_pubchem_compound_url(self):
        graph = _graph(_molecular_state())
        chem = _by_id(graph, "https://pubchem.ncbi.nlm.nih.gov/compound/441764")
        assert chem is not None, "MolecularEntity @id must be the PubChem compound URL"

    def test_identifier_array_order_cas_then_cid(self):
        graph = _graph(_molecular_state())
        chem = _by_id(graph, "https://pubchem.ncbi.nlm.nih.gov/compound/441764")
        assert chem is not None
        ids = _ref_ids(chem.get("identifier"))
        assert ids == [
            "#param_CAS_27336af8c7",
            "#param_PubChem_CID_bc4c668bae",
        ], f"identifier must be [CAS, PubChem CID] refs, got {ids}"

    def test_cas_property_value_has_no_property_id(self):
        graph = _graph(_molecular_state())
        cas = _by_id(graph, "#param_CAS_27336af8c7")
        assert cas is not None
        assert cas.get("@type") == "PropertyValue"
        assert cas.get("name") == "CAS"
        assert cas.get("value") == "33889-69-9"
        assert "propertyID" not in cas, "CAS PropertyValue carries no propertyID (gold)"

    def test_pubchem_cid_property_value_has_property_id_node(self):
        graph = _graph(_molecular_state())
        cid = _by_id(graph, "#param_PubChem_CID_bc4c668bae")
        assert cid is not None
        assert cid.get("@type") == "PropertyValue"
        assert cid.get("name") == "PubChem CID"
        assert cid.get("value") == "441764"
        assert cid.get("propertyID") == {
            "@id": "https://pubchem.ncbi.nlm.nih.gov/compound"
        }

    def test_no_raw_cas_literal_on_node(self):
        graph = _graph(_molecular_state())
        chem = _by_id(graph, "https://pubchem.ncbi.nlm.nih.gov/compound/441764")
        assert chem is not None
        # cas/casrn must not leak onto the node as a bare literal; it lives in the PV.
        assert "cas" not in chem and "casrn" not in chem


class TestPersonAffiliation:
    def test_affiliation_resolves_to_org_reference(self):
        state = CrateState()
        state.metadata.title = "Affiliation crate"
        state.add_entity(
            Entity(
                entity_id="org_vu",
                type="Organization",
                fields={"name": "VU Amsterdam", "ror": "008xxew50"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        state.add_entity(
            Entity(
                entity_id="person_hamers",
                type="Person",
                fields={
                    "name": "Timo Hamers",
                    "orcid": "0000-0002-5733-3290",
                    "affiliation": "org_vu",
                },
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        graph = _graph(state)
        person = _by_id(graph, "https://orcid.org/0000-0002-5733-3290")
        assert person is not None
        assert person.get("affiliation") == {"@id": "https://ror.org/008xxew50"}, (
            "affiliation must be an {@id} reference to the Organization node"
        )

    def test_freetext_affiliation_kept_as_literal(self):
        """A free-text affiliation (no resolvable org / IRI) is valid schema.org and kept."""
        state = CrateState()
        state.metadata.title = "Affiliation crate"
        state.add_entity(
            Entity(
                entity_id="p_lit",
                type="Person",
                fields={"name": "Jane Doe", "affiliation": "Some University"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        graph = _graph(state)
        person = _node_of_type(graph, "Person")
        assert person is not None
        assert person.get("affiliation") == "Some University"


class TestPublicationAuthors:
    def test_scholarly_article_author_references_persons(self):
        state = CrateState()
        state.metadata.title = "Publication crate"
        state.add_entity(
            Entity(
                entity_id="person_a",
                type="Person",
                fields={"name": "Martin Scholze", "orcid": "0000-0002-9569-7562"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        state.add_entity(
            Entity(
                entity_id="pub_mct8",
                type="Publication",
                fields={
                    "name": "MCT8 screening",
                    "doi": "10.1016/j.tiv.2023.105770",
                    "author": ["person_a"],
                },
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        graph = _graph(state)
        article = _by_id(graph, "https://doi.org/10.1016/j.tiv.2023.105770")
        assert article is not None
        assert article.get("@type") == "ScholarlyArticle"
        ids = _ref_ids(article.get("author"))
        assert ids == ["https://orcid.org/0000-0002-9569-7562"], (
            f"author must be an array of Person references, got {ids}"
        )
