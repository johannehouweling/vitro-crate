"""Cells made from a line get the term of what they are in the same call.

`resolve_cell_line` gives a source of cells the ontology term of the organ, tissue
and cell type it is told. Cells made from a line are a sample of that line, and a
name kept apart from the line whose accession it shares is a source of its own.
Both state what they are, and neither took a term until a later call asked about
every cell in the crate. The agent makes no such call unless it thinks of it.
"""

from __future__ import annotations

from typing import Any

import pytest

import builder.tools.composites as composites
from builder.state import CrateState, Entity
from builder.tools.composites import resolve_cell_line

LINE = "SBAD3-01"
ACCESSION = "CVCL_ZX55"
PTL = "Proximal tubular-like cells (PTL-SBAD3-01)"
ELC = "Endothelial-like cells (ELC-SBAD3-01)"
PTL_STATES = {"cell_type": "Epithelial cell of proximal tubule", "tissue": "Proximal tubule"}
ELC_STATES = {"cell_type": "Endothelial cell", "tissue": "Vascular endothelium"}
ENDOTHELIAL = "http://purl.obolibrary.org/obo/CL_0000115"
MISS: dict[str, Any] = {"found": False, "data": {}, "error": "no such record", "transient": False}


@pytest.fixture(autouse=True)
def lookups(monkeypatch) -> list[tuple[str, str]]:
    """Cellosaurus knows the line, the ontology service one cell type. Returns what was asked."""
    asked: list[tuple[str, str]] = []

    def by_name(name: str) -> dict[str, Any]:
        if name.casefold() == LINE.casefold():
            return {"found": True, "data": {"accession": ACCESSION}, "error": None}
        return dict(MISS)

    def by_accession(accession: str) -> dict[str, Any]:
        if accession == ACCESSION:
            record = {"name": LINE, "url": f"https://www.cellosaurus.org/{ACCESSION}"}
            return {"found": True, "data": record, "error": None}
        return dict(MISS)

    def term(label: str, ontology: str) -> dict[str, Any]:
        asked.append((label, ontology))
        if (label.casefold(), ontology) == ("endothelial cell", "cl"):
            data = {
                "@id": ENDOTHELIAL,
                "@type": "DefinedTerm",
                "name": "endothelial cell",
                "termCode": "CL:0000115",
            }
            return {"found": True, "data": data, "error": None, "transient": False}
        return dict(MISS)

    monkeypatch.setattr(composites, "lookup_cell_line_by_name", by_name)
    monkeypatch.setattr(composites, "lookup_cell_line", by_accession)
    monkeypatch.setattr(composites, "lookup_exact_term", term)
    return asked


def _named(state: CrateState, name: str) -> Entity:
    (found,) = [e for e in state.list_entities() if e.fields.get("name") == name]
    return found


class TestCellsMadeFromALine:
    def test_they_get_the_term_of_their_cell_type(self) -> None:
        state = CrateState()
        resolve_cell_line(state, ELC, hints=dict(ELC_STATES), made_from=LINE)
        cells = _named(state, ELC)
        assert cells.type == "Sample"
        assert cells.fields["cell_type_term"]["@id"] == ENDOTHELIAL
        status = cells.get_field_status("cell_type_term")
        assert status is not None and (status.status, status.source) == ("verified", "lookup")

    def test_a_word_no_term_is_called_stays_alone(self, lookups) -> None:
        state = CrateState()
        resolve_cell_line(state, ELC, hints=dict(ELC_STATES), made_from=LINE)
        cells = _named(state, ELC)
        assert ("Vascular endothelium", "uberon") in lookups
        assert cells.fields["tissue"] == "Vascular endothelium"
        assert "tissue_term" not in cells.fields

    def test_the_line_takes_nothing_from_them(self) -> None:
        state = CrateState()
        resolve_cell_line(state, ELC, hints=dict(ELC_STATES), made_from=LINE)
        line = _named(state, LINE)
        assert not [key for key in line.fields if key.endswith("_term") or key == "cell_type"]

    def test_a_service_that_fails_does_not_stop_the_call(self, monkeypatch) -> None:
        def broken(label: str, ontology: str) -> dict[str, Any]:
            raise RuntimeError("the ontology service is down")

        monkeypatch.setattr(composites, "lookup_exact_term", broken)
        state = CrateState()
        outcome = resolve_cell_line(state, ELC, hints=dict(ELC_STATES), made_from=LINE)
        cells = state.get_entity(outcome["entity_id"])
        assert cells is not None and cells.fields["cell_type"] == "Endothelial cell"


class TestANameKeptApartFromItsLine:
    def test_it_gets_the_term_of_its_cell_type(self) -> None:
        state = CrateState()
        resolve_cell_line(state, PTL, hints=dict(PTL_STATES), catalog_name=LINE)
        outcome = resolve_cell_line(state, ELC, hints=dict(ELC_STATES), catalog_name=LINE)
        assert "fix" in outcome, "the second name was not kept apart"
        cells = state.get_entity(outcome["entity_id"])
        assert cells is not None and cells.fields["cell_type_term"]["@id"] == ENDOTHELIAL
