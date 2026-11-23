"""Cells made from a line are samples of that line, not the line under another name.

S-VHPS17 measured two populations, proximal tubular-like and endothelial-like
cells, both differentiated from the iPSC line SBAD3-01. The builder knew one
relation between a name and a catalogued line: the name is that line. Two names
with one accession were therefore one cell line under two names, and the second
population was folded into the first and took its cell type along.

The line is the cell line and carries the accession. Each population is a sample
that a preparation step makes from it, with a cell type and tissue of its own.
"""

from __future__ import annotations

from typing import Any

import pytest

import builder.tools.composites as composites
from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import assemble_crate
from builder.tools.composites import draft_process_chain, resolve_cell_line, scaffold_isa_backbone
from builder.tools.drafters import draft_sample

LINE = "SBAD3-01"
ACCESSION = "CVCL_ZX55"
PTL = "Proximal tubular-like cells (PTL-SBAD3-01)"
ELC = "Endothelial-like cells (ELC-SBAD3-01)"
PTL_STATES = {"cell_type": "Epithelial cell of proximal tubule", "tissue": "Proximal tubule"}
ELC_STATES = {"cell_type": "Endothelial cell", "tissue": "Vascular endothelium"}
MISS: dict[str, Any] = {"found": False, "data": {}, "error": "no such record", "transient": False}


@pytest.fixture(autouse=True)
def cellosaurus(monkeypatch) -> list[str]:
    """Cellosaurus knows the line SBAD3-01 and nothing else. Returns the names it was asked."""
    asked: list[str] = []

    def by_name(name: str) -> dict[str, Any]:
        asked.append(name)
        if name.casefold() == LINE.casefold():
            return {"found": True, "data": {"accession": ACCESSION}, "error": None}
        return dict(MISS)

    def by_accession(accession: str) -> dict[str, Any]:
        if accession == ACCESSION:
            record = {"name": LINE, "url": f"https://www.cellosaurus.org/{ACCESSION}"}
            return {"found": True, "data": record, "error": None}
        return dict(MISS)

    monkeypatch.setattr(composites, "lookup_cell_line_by_name", by_name)
    monkeypatch.setattr(composites, "lookup_cell_line", by_accession)
    return asked


def _of(state: CrateState, kind: str) -> list[Entity]:
    return list(state.list_entities(kind))


def _named(state: CrateState, name: str) -> Entity:
    (found,) = [e for e in state.list_entities() if e.fields.get("name") == name]
    return found


class TestAPopulationMadeFromALine:
    def test_it_is_a_sample_of_the_line(self) -> None:
        state = CrateState()
        outcome = resolve_cell_line(state, PTL, hints=dict(PTL_STATES), made_from=LINE)

        (line,) = _of(state, "CellLineSample")
        (population,) = _of(state, "Sample")
        assert (line.fields["name"], line.fields["accession"]) == (LINE, ACCESSION)
        status = line.get_field_status("accession")
        assert status is not None and status.status == "verified"
        assert population.fields["name"] == PTL
        assert population.fields["derives_from"] == line.entity_id
        assert {key: population.fields[key] for key in PTL_STATES} == PTL_STATES
        assert "accession" not in population.fields, "the accession is the line's"
        assert not set(PTL_STATES) & set(line.fields), (
            "what it states is said of it, not of the line"
        )
        assert (outcome["entity_id"], outcome["line_id"]) == (population.entity_id, line.entity_id)
        assert (outcome["name"], outcome["accession"]) == (PTL, ACCESSION)

    def test_two_populations_share_their_line(self, cellosaurus: list[str]) -> None:
        state = CrateState()
        resolve_cell_line(state, PTL, hints=dict(PTL_STATES), made_from=LINE)
        resolve_cell_line(state, ELC, hints=dict(ELC_STATES), made_from=LINE)

        assert [e.fields["name"] for e in _of(state, "CellLineSample")] == [LINE]
        assert sorted(e.fields["name"] for e in _of(state, "Sample")) == sorted([PTL, ELC])
        assert _named(state, PTL).fields["cell_type"] == PTL_STATES["cell_type"]
        assert _named(state, ELC).fields["cell_type"] == ELC_STATES["cell_type"]
        assert set(cellosaurus) == {LINE}, "only the line is looked up, never a population"

    def test_resolving_it_again_changes_nothing(self) -> None:
        state = CrateState()
        first = resolve_cell_line(state, PTL, hints=dict(PTL_STATES), made_from=LINE)
        again = resolve_cell_line(state, PTL, hints=dict(PTL_STATES), made_from=LINE)
        assert again["entity_id"] == first["entity_id"]
        assert (len(_of(state, "CellLineSample")), len(_of(state, "Sample"))) == (1, 1)

    def test_a_line_cellosaurus_does_not_hold_is_still_its_line(self) -> None:
        state = CrateState()
        outcome = resolve_cell_line(state, PTL, hints=dict(PTL_STATES), made_from="In-house line 7")
        (line,) = _of(state, "CellLineSample")
        assert (line.fields["name"], line.fields.get("accession")) == ("In-house line 7", None)
        assert _named(state, PTL).fields["derives_from"] == line.entity_id
        assert outcome["accession"] == ""

    def test_an_accession_is_not_the_name_of_a_line(self, cellosaurus: list[str]) -> None:
        """An identifier comes back from a lookup and is never handed in (D5)."""
        state = CrateState()
        resolve_cell_line(state, PTL, hints=dict(PTL_STATES), made_from=ACCESSION)
        assert ACCESSION not in cellosaurus
        assert not [e for e in state.list_entities() if e.fields.get("accession")]
        assert _named(state, PTL).type == "CellLineSample", "resolved as a source of its own"


class TestTwoNamesWithOneAccession:
    def test_two_names_that_state_nothing_different_are_one_line(self) -> None:
        state = CrateState()
        resolve_cell_line(state, "SBAD3-01 iPSC", catalog_name=LINE)
        resolve_cell_line(state, "SBAD3-01 induced pluripotent stem cells", catalog_name=LINE)
        (line,) = _of(state, "CellLineSample")
        assert "SBAD3-01 induced pluripotent stem cells" in line.fields["alternateName"]

    def test_two_names_that_state_different_cells_are_not_one_line(self) -> None:
        state = CrateState()
        first = resolve_cell_line(state, PTL, hints=dict(PTL_STATES), catalog_name=LINE)
        second = resolve_cell_line(state, ELC, hints=dict(ELC_STATES), catalog_name=LINE)

        ptl, elc = _named(state, PTL), _named(state, ELC)
        assert ptl.entity_id != elc.entity_id
        assert {key: ptl.fields[key] for key in PTL_STATES} == PTL_STATES, "nothing is overwritten"
        assert {key: elc.fields[key] for key in ELC_STATES} == ELC_STATES
        assert ELC not in (ptl.fields.get("alternateName") or [])
        assert (first["accession"], second["accession"]) == (ACCESSION, "")
        assert "accession" not in elc.fields
        assert "made_from" in second["fix"] and LINE in second["fix"]


def _graph(state: CrateState) -> list[dict[str, Any]]:
    crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    return crate.metadata.generate()["@graph"]


def _ids(value: Any) -> list[str]:
    listed = value if isinstance(value, list) else [] if value is None else [value]
    return [item["@id"] for item in listed]


class TestTheCrate:
    def test_a_sample_states_its_own_characteristics(self) -> None:
        state = CrateState()
        draft_sample(state, {"name": PTL, **PTL_STATES, "organ": "Kidney"})
        graph = _graph(state)
        (sample,) = [n for n in graph if "Sample" in n["@type"]]
        stated = {
            n["name"]: n["value"]
            for n in graph
            if n["@id"] in _ids(sample.get("additionalProperty"))
        }
        assert stated == {
            "cell type": "Epithelial cell of proximal tubule",
            "Tissue": "Proximal tubule",
            "Organ": "Kidney",
        }
        assert not {"cell_type", "tissue", "organ"} & set(sample)

    def test_each_population_is_made_from_the_line_by_its_own_step(self) -> None:
        state = CrateState()
        state.metadata.title = "TempO-Seq transcriptomics of iPSC-derived cells"
        backbone = scaffold_isa_backbone(state, assay={"name": "TempO-Seq"})
        ptl = resolve_cell_line(state, PTL, hints=dict(PTL_STATES), made_from=LINE)
        elc = resolve_cell_line(state, ELC, hints=dict(ELC_STATES), made_from=LINE)
        counts = Entity(
            entity_id="raw_counts",
            type="File",
            fields={"name": "read_counts.csv", "path": "raw/read_counts.csv"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(counts)
        made = {"cell_line": ptl["line_id"], "preparation_type": "differentiation"}
        steps = [
            {"hints": {"name": "Differentiation to PTL", **made}, "result": ptl["entity_id"]},
            {"hints": {"name": "Differentiation to ELC", **made}, "result": elc["entity_id"]},
        ]
        draft_process_chain(
            state,
            backbone["assay_id"],
            chain=[
                *({"process_type": "TestSystemPreparation", **step} for step in steps),
                {
                    "process_type": "EndpointReadout",
                    "hints": {"name": "TempO-Seq readout"},
                    "result": counts.entity_id,
                },
            ],
        )

        graph = _graph(state)
        by_name = {n.get("name"): n for n in graph}
        line = by_name[LINE]
        assert line["identifier"] == ACCESSION
        for population, step in ((PTL, "Differentiation to PTL"), (ELC, "Differentiation to ELC")):
            assert _ids(by_name[population]["derivesFrom"]) == [line["@id"]]
            assert _ids(by_name[step].get("input")) == [line["@id"]]
            assert _ids(by_name[step].get("output")) == [by_name[population]["@id"]]
        measured = _ids(by_name["TempO-Seq readout"].get("input"))
        assert sorted(measured) == sorted([by_name[PTL]["@id"], by_name[ELC]["@id"]])


