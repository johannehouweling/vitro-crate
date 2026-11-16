"""A step states any parameter the deposit names, under the name the deposit gives it.

A step used to carry only the parameters this tool has a field for: an exposure's
duration, a readout's instrument. Replicates, a positive control, a time point, the
exposure type or a wavelength had no place, so a crate said less than the deposit
did. ``parameters`` is that place: each entry is written as a ParameterValue of the
step, named as stated, with its unit.
"""

from __future__ import annotations

import warnings
from typing import Any

from builder.agents.pipeline.leaves import _process_parameters_schema
from builder.agents.pipeline.pipeline import _merge_plan_chain_names
from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import draft_hints_schema
from builder.tools.builder import assemble_crate
from builder.tools.drafters import draft_process


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _state(**exposure: Any) -> CrateState:
    s = CrateState()
    s.metadata.title = "T"
    s.add_entity(_ent("inv_1", "Investigation", name="T", description="d"))
    s.add_entity(_ent("study_1", "Study", name="S", investigation_id="inv_1"))
    s.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
    s.add_entity(_ent("chem_1", "MolecularEntity", name="Iopanoic acid"))
    s.add_entity(
        _ent(
            "proc_exp",
            "LabProcess",
            name="Exposure",
            process_type="Exposure",
            assay_id="assay_1",
            chemicals="chem_1",
            **exposure,
        )
    )
    return s


def _graph(state: CrateState) -> dict[str, dict]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    return {node["@id"]: node for node in crate.metadata.generate()["@graph"]}


def _listed(value) -> list:
    return value if isinstance(value, list) else [] if value is None else [value]


def _parameters(nodes: dict[str, dict], key: str = "parameter") -> dict[str, dict]:
    """The step's parameters by name."""
    step = next(n for n in nodes.values() if n.get("name") == "Exposure" and "parameter" in n)
    return {nodes[ref["@id"]]["name"]: nodes[ref["@id"]] for ref in _listed(step.get(key))}


STATED = [
    {"name": "Exposure type", "value": "single"},
    {"name": "Exposure-measurement interval", "value": "24", "unit": "hours"},
]


class TestTheCrate:
    def test_a_stated_parameter_is_a_parameter_of_the_step(self):
        found = _parameters(_graph(_state(duration="30 minutes", parameters=STATED)))
        assert found["Exposure type"]["value"] == "single"
        assert found["Exposure-measurement interval"]["value"] == "24"
        assert found["Exposure-measurement interval"]["unitText"] == "hours"
        assert "unitText" not in found["Exposure type"]
        assert found["Exposure Duration"]["value"] == "30 minutes"

    def test_it_reaches_both_predicates_the_profiles_read(self):
        nodes = _graph(_state(duration="30 minutes", parameters=STATED))
        assert set(_parameters(nodes, "parameterValue")) == set(_parameters(nodes, "parameter"))

    def test_a_mapping_of_names_to_values_is_read_too(self):
        found = _parameters(_graph(_state(duration="30 minutes", parameters={"Replicates": "3"})))
        assert found["Replicates"]["value"] == "3"

    def test_a_parameter_this_tool_has_a_field_for_is_written_once(self):
        again = [{"name": "exposure duration", "value": "one hour"}]
        found = _parameters(_graph(_state(duration="30 minutes", parameters=again)))
        assert [name for name in found if name.casefold() == "exposure duration"] == ["Exposure Duration"]
        assert found["Exposure Duration"]["value"] == "30 minutes"

    def test_a_value_that_says_nothing_is_not_written(self):
        silent = [{"name": "Vehicle", "value": "unknown"}, {"name": "Recovery period", "value": ""}]
        found = _parameters(_graph(_state(duration="30 minutes", parameters=silent)))
        assert set(found) == {"Exposure Duration"}

    def test_the_list_itself_is_not_kept_as_a_property(self):
        nodes = _graph(_state(duration="30 minutes", parameters=STATED))
        assert not [n for n in nodes.values() if n.get("name") == "parameters"]
        step = next(n for n in nodes.values() if n.get("name") == "Exposure" and "parameter" in n)
        assert "additionalProperty" not in step

    def test_a_step_with_only_stated_parameters_has_them(self):
        found = _parameters(_graph(_state(parameters=STATED)))
        assert set(found) == {"Exposure type", "Exposure-measurement interval"}


class TestTheTools:
    def test_the_drafter_offers_the_field(self):
        offered = draft_hints_schema("LabProcess")["properties"]["parameters"]
        assert offered["type"] == "array"
        assert set(offered["items"]["properties"]) == {"name", "value", "unit"}
        assert offered["items"]["required"] == ["name", "value"]

    def test_a_drafted_step_keeps_what_it_was_given(self):
        state = _state()
        step = draft_process(state, "assay_1", "EndpointReadout", {"name": "Readout", "parameters": STATED})
        assert step.fields["parameters"] == STATED


class TestThePlan:
    def test_the_plan_can_name_other_parameters(self):
        offered = _process_parameters_schema()["properties"]["other"]
        assert offered["type"] == "array"
        assert set(offered["items"]["properties"]) == {"name", "value", "unit"}

    def test_they_reach_the_step(self):
        plan = {
            "process_chain": [
                {
                    "process_type": "EndpointReadout",
                    "parameters": {
                        "endpoint": "T3 uptake",
                        "other": [
                            {"name": "Positive control", "value": "Silychristin"},
                            {"name": "Time point", "value": "130", "unit": "minutes"},
                        ],
                    },
                }
            ]
        }
        readout = next(s for s in _merge_plan_chain_names(plan) if s["process_type"] == "EndpointReadout")
        assert readout["hints"]["endpoint"] == "T3 uptake"
        assert readout["hints"]["parameters"] == [
            {"name": "Positive control", "value": "Silychristin"},
            {"name": "Time point", "value": "130", "unit": "minutes"},
        ]

    def test_an_identifier_is_not_a_parameter(self):
        """Identifiers come from lookups, never from the model's reading (D5)."""
        plan = {
            "process_chain": [
                {
                    "process_type": "EndpointReadout",
                    "parameters": {
                        "other": [
                            {"name": "CAS", "value": "51-48-9"},
                            {"name": "DOI", "value": "10.1000/x"},
                            {"name": "Replicates", "value": "3"},
                            {"name": "", "value": "nameless"},
                            {"name": "Blank", "value": " "},
                            "not a parameter",
                        ]
                    },
                }
            ]
        }
        readout = next(s for s in _merge_plan_chain_names(plan) if s["process_type"] == "EndpointReadout")
        assert readout["hints"]["parameters"] == [{"name": "Replicates", "value": "3"}]

    def test_a_plan_without_them_adds_no_field(self):
        steps = _merge_plan_chain_names({"process_chain": [{"process_type": "Exposure", "name": "E"}]})
        assert all("parameters" not in step["hints"] for step in steps)
