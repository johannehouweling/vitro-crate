"""A readout's substrate that is a chemical is a compound of its own.

A record states the substrate of a readout with its CAS number: the labelled
hormone whose uptake an assay measures. The builder kept a substrate as a name on
the readout, so its identifiers had no place in a crate, and a record written
from one lost them. The name stays the readout's `Substrate` parameter. The
chemical is looked up like any compound and is a reagent of the readout's
protocol record. It is not a test chemical: the exposure does not apply it.
"""

from __future__ import annotations

from typing import Any

import pytest

from builder.state import CrateState
from builder.tools._crate_mapping import draft_hints_schema
from builder.tools.builder import assemble_crate
from builder.tools.composites import (
    draft_process_chain,
    scaffold_isa_backbone,
    wire_unreferenced_domain_entities,
)
from builder.tools.drafters import draft_molecular_entity
from builder.tools.provenance import link

T3 = "3,3',5-triiodo-L-thyronine"
SILYCHRISTIN = "Silychristin"
DMSO = "Dimethyl sulfoxide"


def _study(*, substrate: bool = True) -> tuple[CrateState, dict[str, str]]:
    """An assay with an exposure to a test chemical and a readout, with or without a substrate."""
    state = CrateState()
    state.metadata.title = "Inhibition of T3 uptake"
    backbone = scaffold_isa_backbone(state, assay={"name": "T3 uptake"})
    test = draft_molecular_entity(
        state, SILYCHRISTIN, {"cas": "33889-69-9", "pubchem_cid": "441764"}
    )
    measured = draft_molecular_entity(state, T3, {"cas": "6893-02-3", "pubchem_cid": "5920"})
    reads: dict[str, Any] = {"name": "Uptake readout", "endpoint": "T3 uptake"}
    if substrate:
        reads |= {"substrate": T3, "reagents": [measured.entity_id]}
    chain = draft_process_chain(
        state,
        backbone["assay_id"],
        chain=[
            {"process_type": "TestSystemPreparation", "hints": {"name": "Culture"}},
            {
                "process_type": "Exposure",
                "hints": {"name": "Exposure", "chemicals": [test.entity_id], "duration": "30 min"},
            },
            {"process_type": "EndpointReadout", "hints": reads, "result": "raw_counts"},
        ],
    )
    ids = {step["process_type"]: step["process_id"] for step in chain["steps"]}
    return state, {**ids, "test": test.entity_id, "substrate": measured.entity_id}


def _graph(state: CrateState) -> list[dict[str, Any]]:
    crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    return crate.metadata.generate()["@graph"]


def _ids(value: Any) -> list[str]:
    listed = value if isinstance(value, list) else [] if value is None else [value]
    return [item["@id"] for item in listed]


def _reagents_of(graph: list[dict[str, Any]], step_name: str) -> list[str]:
    """The names of the reagents the protocol record of the step called *step_name* lists."""
    by_id = {node["@id"]: node for node in graph}
    (step,) = [n for n in graph if n.get("name") == step_name and "LabProcess" in n["@type"]]
    record = by_id[step["executesLabProtocol"]["@id"]]
    return [by_id[i]["name"] for i in _ids(record.get("reagent"))]


class TestTheCrate:
    def test_the_substrate_is_a_reagent_of_the_readout_s_record(self) -> None:
        state, _ = _study()
        graph = _graph(state)
        assert _reagents_of(graph, "Uptake readout") == [T3]
        (compound,) = [
            n for n in graph if n.get("name") == T3 and "MolecularEntity" in str(n["@type"])
        ]
        stated = {
            n["name"]: n["value"] for n in graph if n["@id"] in _ids(compound.get("identifier"))
        }
        assert stated["CAS"] == "6893-02-3"

    def test_the_readout_still_names_its_substrate(self) -> None:
        state, _ = _study()
        graph = _graph(state)
        named = [
            n["value"]
            for n in graph
            if n.get("name") == "Substrate" and n.get("additionalType") == "ParameterValue"
        ]
        assert named == [T3]

    def test_the_exposure_does_not_apply_it(self) -> None:
        state, _ = _study()
        assert _reagents_of(_graph(state), "Exposure") == [SILYCHRISTIN]

    def test_a_readout_without_a_substrate_lists_no_reagent(self) -> None:
        state, _ = _study(substrate=False)
        assert _reagents_of(_graph(state), "Uptake readout") == []

    @pytest.mark.timeout(600)
    def test_it_earns_the_crate_no_finding(self) -> None:
        from builder.tools.validation import build_and_validate

        def findings(*, substrate: bool) -> set[tuple[str, str]]:
            state, _ = _study(substrate=substrate)
            issues = build_and_validate(state, severity="optional", profile="all")["issues"]
            return {(str(i.get("profile")), str(i.get("message"))) for i in issues}

        assert findings(substrate=True) <= findings(substrate=False)


class TestTheWiring:
    def test_a_substrate_is_not_taken_for_a_chemical_nobody_wired(self) -> None:
        """The export wires a compound no step uses onto the exposure. A substrate is used."""
        state, ids = _study()
        wire_unreferenced_domain_entities(state)
        exposure = state.get_entity(ids["Exposure"])
        assert exposure is not None and ids["substrate"] not in list(exposure.fields["chemicals"])

    def test_linking_a_compound_to_a_readout_makes_it_its_reagent(self) -> None:
        state, ids = _study(substrate=False)
        link(state, ids["EndpointReadout"], "input", ids["substrate"])
        readout = state.get_entity(ids["EndpointReadout"])
        assert readout is not None
        held = readout.fields.get("reagents")
        assert (held if isinstance(held, list) else [held]) == [ids["substrate"]]
        assert _reagents_of(_graph(state), "Uptake readout") == [T3]

    def test_a_step_is_told_it_can_name_its_reagents(self) -> None:
        offered = draft_hints_schema("LabProcess")["properties"]
        assert "resolve_compound" in offered["reagents"]["description"]


