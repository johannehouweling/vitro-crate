"""A crate whose chemical sits at its Compound Wiki address is written and validated as before.

The S-VHPS21 fixture's one chemical, triiodothyronine, is given the fields its
lookups write: the CAS number, PubChem CID and InChIKey PubChem states, and the
item the Compound Wiki states that InChIKey on. The crate is exported once with
that item and once without it, so any difference between the two is one the
address makes.

Offline: the fixture is a recorded state and no lookup runs. The values are the
ones the PubChem record and the Compound Wiki item ``Q5027`` state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.state import CrateState
from builder.tools._crate_mapping import _mint_id
from builder.tools.builder import export_crate
from tests.fixtures.vhps_golden_crates import vhps_fixture_state

pytestmark = pytest.mark.timeout(180)

WIKI = "https://compoundcloud.wikibase.cloud/entity/Q5027"
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/compound/5920"
LOOKED_UP = {"cas": "6893-02-3", "pubchem_cid": "5920", "inchikey": "AUYYCJSJGJYCDS-LBPRGKRZSA-N"}


def _references(node: object) -> list[str]:
    """Every one-key ``{"@id": ...}`` reference anywhere inside *node*."""
    if isinstance(node, dict):
        own = [node["@id"]] if set(node) == {"@id"} else []
        return own + [
            ref for key, value in node.items() if key != "@id" for ref in _references(value)
        ]
    if isinstance(node, list):
        return [ref for item in node for ref in _references(item)]
    return []


def _export(tmp: Path, *, wiki_id: str | None) -> tuple[CrateState, list[dict], str]:
    """The fixture with its chemical looked up, exported to *tmp*: state, graph, chemical id."""
    state = vhps_fixture_state("S-VHPS21")
    (chem,) = state.list_entities("MolecularEntity")
    chem.set_fields_from_dict(LOOKED_UP, source="lookup")
    if wiki_id is not None:
        chem.set_fields_from_dict({"compoundwiki_id": wiki_id}, source="lookup")
    out = tmp / "crate"
    state.metadata.output_path = str(out)
    result = export_crate(state, str(out), embed_report=False)
    assert result["success"], result["error"]
    graph = json.loads((out / "ro-crate-metadata.json").read_text(encoding="utf-8"))["@graph"]
    return state, graph, _mint_id(chem)


@pytest.fixture(scope="module")
def at_wiki(tmp_path_factory: pytest.TempPathFactory) -> tuple[CrateState, list[dict], str]:
    return _export(tmp_path_factory.mktemp("wiki"), wiki_id="Q5027")


@pytest.fixture(scope="module")
def at_pubchem(tmp_path_factory: pytest.TempPathFactory) -> tuple[CrateState, list[dict], str]:
    return _export(tmp_path_factory.mktemp("pubchem"), wiki_id=None)


def _holders(graph: list[dict], target: str) -> list[tuple[str, str]]:
    """The (node, property) pairs whose value references *target*."""
    return sorted(
        (node["@id"], key)
        for node in graph
        for key, value in node.items()
        if key != "@id" and target in _references(value)
    )


def _verdict(state: CrateState, chem_id: str) -> tuple[tuple[bool, bool, bool], list[tuple]]:
    """The recorded pass of each profile and every finding, the chemical's id put aside."""
    report = state.validation
    findings = sorted(
        (
            record["profile"],
            record["severity"],
            str(record["entity_id"]).replace(chem_id, "<chemical>"),
            str(record["message"]).replace(chem_id, "<chemical>"),
        )
        for record in report.issue_records
    )
    return (report.base_passed, report.isa_passed, report.tox_passed), findings


class TestACrateWithAChemicalAtItsWikiAddress:
    def test_the_chemical_is_written_at_its_item(self, at_wiki):
        _, graph, chem_id = at_wiki
        assert chem_id == WIKI
        (node,) = [n for n in graph if "MolecularEntity" in n.get("@type", [])]
        assert node["@id"] == WIKI
        assert node["name"] == "Triiodothyronine"
        assert PUBCHEM not in {n["@id"] for n in graph}

    def test_everything_that_named_the_chemical_names_it_there(self, at_wiki, at_pubchem):
        """The protocol record, the condition table and its compound column follow the address."""
        _, graph, _ = at_wiki
        _, pubchem_graph, _ = at_pubchem
        holders = _holders(graph, WIKI)
        assert ("#LabProcess_exposure_protocol", "reagent") in holders
        assert holders == _holders(pubchem_graph, PUBCHEM)
        assert PUBCHEM not in _references(graph)

    def test_the_verdict_is_the_one_at_the_pubchem_address(self, at_wiki, at_pubchem):
        wiki_verdict = _verdict(at_wiki[0], at_wiki[2])
        assert wiki_verdict[0] == (True, True, True)
        assert wiki_verdict == _verdict(at_pubchem[0], at_pubchem[2])
