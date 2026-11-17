"""The species of a source of cells carries its taxonomy term when one is called that.

A cell line takes its organism from its Cellosaurus record, as a term. Primary
cells have no record, so their species is the word the documents use, and a
record written from the crate stated "Homo sapiens" without the NCBITaxon term
every other record gives. The lookup that gives a stated organ its term asks
NCBITaxon the same question. Here the term is the value: `taxonomicRange` holds
a term for a cell line, and holds this one the same way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import responses

import builder.tools.composites as composites
from builder.state import CrateState, Entity
from builder.tools._crate_mapping import STATED_TERM_ONTOLOGIES, TERM_FIELDS, term_field
from builder.tools.builder import assemble_crate
from builder.tools.composites import resolve_cell_line, resolve_characteristic_terms
from builder.tools.drafters import draft_cell_line_sample
from builder.tools.field_kinds import drafter_visible_fields, is_identifier_field
from builder.tools.management import set_fields
from lookups._http import reset_circuit_breaker, reset_host_throttle
from lookups.bao import lookup_exact_term

FIXTURES = Path(__file__).parent / "fixtures"
OLS = "https://www.ebi.ac.uk/ols4/api/search"
HUMAN = {
    "@id": "http://purl.obolibrary.org/obo/NCBITaxon_9606",
    "@type": "DefinedTerm",
    "name": "Homo sapiens",
    "termCode": "NCBITaxon:9606",
}
HELD = term_field("taxonomicRange")


@pytest.fixture(autouse=True)
def _fresh_lookup():
    lookup_exact_term.cache_clear()
    reset_host_throttle()
    reset_circuit_breaker()
    yield
    lookup_exact_term.cache_clear()
    reset_circuit_breaker()


def _known(monkeypatch: pytest.MonkeyPatch, *words: str) -> list[tuple[str, str]]:
    """The taxonomy calls each of *words* a human; return what the composite asked."""
    asked: list[tuple[str, str]] = []

    def answer(label: str, ontology: str) -> dict[str, Any]:
        asked.append((label, ontology))
        if ontology == "ncbitaxon" and label.casefold() in {w.casefold() for w in words}:
            return {"found": True, "data": dict(HUMAN), "error": None}
        return {"found": False, "data": {}, "error": "no such term", "transient": False}

    monkeypatch.setattr(composites, "lookup_exact_term", answer)
    return asked


def _donor_cells(state: CrateState, species: Any = "Homo sapiens") -> Entity:
    stated = {"source_kind": "primary cells", "taxonomicRange": species}
    return draft_cell_line_sample(state, "donor cells", stated)


def _cells_node(state: CrateState) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    graph = assemble_crate(state, output_dir=None, materialize_payload=False).metadata.generate()
    by_id = {node["@id"]: node for node in graph["@graph"]}
    (cells,) = [n for n in by_id.values() if n.get("name") == "donor cells"]
    return cells, by_id


class TestTheLookup:
    @responses.activate
    def test_a_species_name_gets_its_taxonomy_term(self) -> None:
        recorded = json.loads((FIXTURES / "ols_exact_ncbitaxon_homo_sapiens.json").read_text())
        responses.add(responses.GET, OLS, json=recorded)
        assert lookup_exact_term("Homo sapiens", "ncbitaxon") == HUMAN

    @responses.activate
    def test_the_common_name_the_taxonomy_marks_exact_gets_it_too(self) -> None:
        recorded = json.loads((FIXTURES / "ols_exact_ncbitaxon_homo_sapiens.json").read_text())
        responses.add(responses.GET, OLS, json=recorded)
        assert lookup_exact_term("human", "ncbitaxon") == HUMAN


class TestTheComposite:
    def test_the_stated_species_of_primary_cells_gets_its_term(self, monkeypatch) -> None:
        asked = _known(monkeypatch, "Homo sapiens")
        state = CrateState()
        cells = _donor_cells(state)
        outcome = resolve_characteristic_terms(state, cells.entity_id)
        assert ("Homo sapiens", "ncbitaxon") in asked
        assert cells.fields["taxonomicRange"] == "Homo sapiens", "the word is kept"
        assert cells.fields[HELD] == {**HUMAN, "confirmedFor": "Homo sapiens"}
        status = cells.get_field_status(HELD)
        assert status is not None and (status.status, status.source) == ("verified", "lookup")
        assert {"entity_id": cells.entity_id, "field": "taxonomicRange"}.items() <= (
            outcome["found"][0].items()
        )

    def test_resolving_the_cells_asks_in_the_same_call(self, monkeypatch) -> None:
        _known(monkeypatch, "Homo sapiens")
        state = CrateState()
        told = {"source_kind": "primary cells", "taxonomicRange": "Homo sapiens"}
        outcome = resolve_cell_line(state, "donor cells", hints=told)
        cells = state.get_entity(outcome["entity_id"])
        assert cells is not None and cells.fields[HELD]["@id"] == HUMAN["@id"]

    def test_an_organism_a_record_gave_is_not_asked_about(self, monkeypatch) -> None:
        asked = _known(monkeypatch, "Homo sapiens")
        state = CrateState()
        cells = _donor_cells(state, {"@id": HUMAN["@id"], "name": "Homo sapiens"})
        resolve_characteristic_terms(state, cells.entity_id)
        assert not [word for word, ontology in asked if ontology == "ncbitaxon"]
        assert HELD not in cells.fields

    def test_the_term_fields_are_the_characteristics_and_the_species(self) -> None:
        assert TERM_FIELDS == {"organ_term", "tissue_term", "cell_type_term", HELD}
        assert TERM_FIELDS == {term_field(field) for field in STATED_TERM_ONTOLOGIES}

    def test_it_is_an_identifier_no_model_is_asked_for(self) -> None:
        assert is_identifier_field(HELD)
        assert HELD not in drafter_visible_fields("CellLineSample")

    def test_a_caller_cannot_hand_the_term_in(self) -> None:
        state = CrateState()
        told = {"source_kind": "primary cells", "taxonomicRange": "Homo sapiens", HELD: HUMAN}
        outcome = resolve_cell_line(state, "donor cells", hints=told)
        cells = state.get_entity(outcome["entity_id"])
        assert cells is not None and HELD not in cells.fields


class TestTheCrate:
    def test_the_species_is_written_as_the_term(self, monkeypatch) -> None:
        _known(monkeypatch, "Homo sapiens")
        state = CrateState()
        resolve_characteristic_terms(state, _donor_cells(state).entity_id)
        cells, by_id = _cells_node(state)
        assert cells["taxonomicRange"] == {"@id": HUMAN["@id"]}
        term = by_id[HUMAN["@id"]]
        assert (term["@type"], term["name"]) == ("DefinedTerm", "Homo sapiens")

    def test_a_species_no_term_is_called_stays_a_word(self, monkeypatch) -> None:
        _known(monkeypatch)
        state = CrateState()
        resolve_characteristic_terms(state, _donor_cells(state, "Homo sapiens sapiens").entity_id)
        cells, _ = _cells_node(state)
        assert cells["taxonomicRange"] == "Homo sapiens sapiens"

    def test_a_species_edited_since_is_written_as_the_new_word(self, monkeypatch) -> None:
        _known(monkeypatch, "Homo sapiens")
        state = CrateState()
        cells = _donor_cells(state)
        resolve_characteristic_terms(state, cells.entity_id)
        set_fields(state, cells.entity_id, {"taxonomicRange": "Sus scrofa"})
        node, by_id = _cells_node(state)
        assert node["taxonomicRange"] == "Sus scrofa"
        assert HUMAN["@id"] not in by_id
