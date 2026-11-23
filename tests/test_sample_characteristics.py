"""Source cells state the characteristics the profile expects of them.

The specification lists them (Sample - Source Cells): the supplier, the catalogue
and lot number of commercially sourced material, and the donor of primary cells.
A crate kept such a value only as a bare PropertyValue named after its field,
with no term saying what it is, and the build was not told it could state one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from builder.state import CrateState
from builder.tools._crate_mapping import _CELL_LINE_CHARACTERISTICS, draft_hints_schema
from builder.tools.builder import assemble_crate
from builder.tools.drafters import draft_cell_line_sample, draft_molecular_entity
from builder.tools.field_kinds import drafter_visible_fields

SPEC = Path(__file__).resolve().parents[1] / "profiles" / "docs" / "isa_tox.md"
NCIT = "http://purl.obolibrary.org/obo/NCIT_"
STATED = {
    "supplier": ("supplier", "ATCC", NCIT + "C90473"),
    "catalogue_number": ("catalogue number", "HB-8065", NCIT + "C99286"),
    "lot_number": ("lot number", "70012345", NCIT + "C70848"),
    "donor_code": ("donor code", "T19", None),
    "number_of_donors": ("number of donors", "3", None),
    "donor_age": ("donor age", "40 to 50 years", "http://www.ebi.ac.uk/efo/EFO_0000246"),
    "donor_health_status": ("donor health status", "healthy", None),
}


def _graph(**hints: Any) -> list[dict[str, Any]]:
    state = CrateState()
    hints = {"source_kind": "primary cells", **hints}
    draft_cell_line_sample(state, "kidney tubuloids, donor T19", hints)
    crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    return crate.metadata.generate()["@graph"]


def _sample(graph: list[dict[str, Any]]) -> dict[str, Any]:
    (sample,) = [n for n in graph if "Sample" in n["@type"]]
    return sample


def _characteristics(graph: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    listed = _sample(graph).get("additionalProperty") or []
    ids = {ref["@id"] for ref in (listed if isinstance(listed, list) else [listed])}
    return {n["name"]: n for n in graph if n["@id"] in ids}


@pytest.mark.parametrize("field", sorted(STATED))
def test_a_stated_value_is_a_characteristic_with_its_term(field: str) -> None:
    name, value, term = STATED[field]
    graph = _graph(**{field: value})
    found = _characteristics(graph)[name]
    assert (found["additionalType"], found["value"]) == ("CharacteristicValue", value)
    assert found.get("propertyID") == ({"@id": term} if term else None)
    assert field not in _sample(graph), "the value is stated once, as the characteristic"


def test_the_american_spelling_states_the_same_characteristic() -> None:
    stated = _characteristics(_graph(catalog_number="HB-8065"))
    assert stated["catalogue number"]["value"] == "HB-8065"


def test_donor_sex_is_the_sample_s_own_property() -> None:
    """The profile puts it on the BioSample, not among the characteristics."""
    graph = _graph(sex="female")
    assert _sample(graph)["sex"] == "female"
    assert "sex" not in _characteristics(graph)


def test_nothing_is_stated_of_a_source_that_states_nothing() -> None:
    graph = _graph()
    assert _characteristics(graph) == {}
    assert "sex" not in _sample(graph)


def test_a_chemical_keeps_its_own_supplier_and_lot() -> None:
    """They are characteristics of a source of cells, and of nothing else."""
    state = CrateState()
    draft_molecular_entity(state, "Desipramine", {"supplier": "Sigma", "lot_number": "L1"})
    crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    graph = crate.metadata.generate()["@graph"]
    kept = {n["name"]: n for n in graph if n["@type"] == "PropertyValue"}
    assert (kept["supplier"]["value"], kept["lot number"]["value"]) == ("Sigma", "L1")
    assert "additionalType" not in kept["supplier"]


def test_the_builder_writes_the_characteristics_the_specification_lists() -> None:
    """One list, kept in two places: a name or a term changed in one must change in the other."""
    section = SPEC.read_text(encoding="utf-8").split("#### Source Cells - Primary Cells")[1]
    table = section.split("**Expected `additionalProperty` items:**")[1].split("\n### ")[0]
    listed = {
        name: (term.strip("`") if term.startswith("`") else None)
        for name, term in re.findall(r"^\|([^|]+)\|(?:SHOULD|MAY)\|([^|]*)\|", table, re.MULTILINE)
    }
    assert len(listed) == 12
    assert {c.name: c.property_id for c in _CELL_LINE_CHARACTERISTICS} == listed


def test_both_arms_are_told_what_a_source_can_state() -> None:
    """The hint schema is what the agent reads and what the drafter is offered."""
    fields = {alias for c in _CELL_LINE_CHARACTERISTICS for alias in c.aliases[:1]} | {"sex"}
    assert fields <= set(draft_hints_schema("CellLineSample")["properties"])
    assert fields <= drafter_visible_fields("CellLineSample")
