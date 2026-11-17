"""A step belongs to the assay it was drafted for.

A deposit with two assays has two exposures. A step's entity was named after the
step's name alone, so the second assay's "Exposure" was the first assay's
"Exposure": drafting it replaced the first one and moved it to the second assay.
The first assay kept only the steps it happened to name differently, and lost
its culture, its exposure and its analysis without a word.

Two assays may name a step alike. Each keeps its own.
"""

from __future__ import annotations

from typing import Any

from builder.state import CrateState, Entity
from builder.tools.builder import assemble_crate
from builder.tools.composites import draft_process_chain, scaffold_isa_backbone
from builder.tools.drafters import draft_assay, draft_process

UPTAKE = "Thyroxine uptake"
VIABILITY = "Cell viability"


def _two_assays() -> tuple[CrateState, str, str]:
    state = CrateState()
    state.metadata.title = "Inhibition of thyroxine uptake"
    made = scaffold_isa_backbone(state, assay={"name": UPTAKE})
    second = draft_assay(state, made["study_id"], {"name": VIABILITY})
    return state, made["assay_id"], second.entity_id


def _chain(duration: str, result: str) -> list[dict[str, Any]]:
    return [
        {
            "process_type": "TestSystemPreparation",
            "hints": {"name": "Cell culture", "culture_medium": "DMEM/F12"},
        },
        {"process_type": "Exposure", "hints": {"name": "Exposure", "duration": duration}},
        {"process_type": "EndpointReadout", "hints": {"name": "Readout"}, "result": result},
    ]


def _steps_of(state: CrateState, assay_id: str) -> list[Entity]:
    return [s for s in state.list_entities("LabProcess") if s.fields.get("assay_id") == assay_id]


def _kinds(steps: list[Entity]) -> list[str]:
    return sorted(str(step.fields.get("process_type")) for step in steps)


ALL_THREE = ["EndpointReadout", "Exposure", "TestSystemPreparation"]


class TestTwoAssaysThatNameTheirStepsAlike:
    def test_each_has_its_own_steps(self) -> None:
        state, first, second = _two_assays()
        one = draft_process_chain(state, first, chain=_chain("30 minutes", "counts"))
        two = draft_process_chain(state, second, chain=_chain("24 hours", "luminescence"))
        assert not set(one["process_ids"]) & set(two["process_ids"])
        assert _kinds(_steps_of(state, first)) == ALL_THREE
        assert _kinds(_steps_of(state, second)) == ALL_THREE

    def test_the_first_keeps_what_its_step_stated(self) -> None:
        state, first, second = _two_assays()
        draft_process_chain(state, first, chain=_chain("30 minutes", "counts"))
        draft_process_chain(state, second, chain=_chain("24 hours", "luminescence"))
        stated = {
            assay: [
                s.fields.get("duration") for s in _steps_of(state, assay) if "duration" in s.fields
            ]
            for assay in (first, second)
        }
        assert stated == {first: ["30 minutes"], second: ["24 hours"]}

    def test_drafting_either_chain_again_adds_nothing(self) -> None:
        state, first, second = _two_assays()
        one = draft_process_chain(state, first, chain=_chain("30 minutes", "counts"))
        two = draft_process_chain(state, second, chain=_chain("24 hours", "luminescence"))
        again_two = draft_process_chain(state, second, chain=_chain("24 hours", "luminescence"))
        again_one = draft_process_chain(state, first, chain=_chain("30 minutes", "counts"))
        assert again_one["process_ids"] == one["process_ids"]
        assert again_two["process_ids"] == two["process_ids"]
        assert len(state.list_entities("LabProcess")) == 6

    def test_steps_nobody_named_are_kept_apart_too(self) -> None:
        state, first, second = _two_assays()
        one = draft_process(state, first, "Exposure", {})
        two = draft_process(state, second, "Exposure", {})
        assert one.entity_id != two.entity_id
        assert [s.entity_id for s in _steps_of(state, first)] == [one.entity_id]
        assert [s.entity_id for s in _steps_of(state, second)] == [two.entity_id]


class TestWhatDoesNotChange:
    def test_a_step_keeps_the_identifier_its_name_gives(self) -> None:
        state, first, _ = _two_assays()
        step = draft_process(state, first, "Exposure", {"name": "Exposure"})
        assert step.entity_id == "proc_exposure"

    def test_one_assay_drafting_a_step_again_has_one_step(self) -> None:
        state, first, _ = _two_assays()
        draft_process(state, first, "Exposure", {"name": "Exposure", "duration": "30 minutes"})
        draft_process(state, first, "Exposure", {"name": "Exposure", "duration": "1 hour"})
        (step,) = _steps_of(state, first)
        assert step.fields["duration"] == "1 hour"

    def test_an_identifier_the_caller_gives_names_that_step(self) -> None:
        state, first, second = _two_assays()
        draft_process(state, first, "Exposure", {"name": "Exposure", "entity_id": "proc_shared"})
        draft_process(state, second, "Exposure", {"name": "Exposure", "entity_id": "proc_shared"})
        assert [s.entity_id for s in state.list_entities("LabProcess")] == ["proc_shared"]


class TestTheCrate:
    def test_each_assay_holds_its_own_steps(self) -> None:
        state, first, second = _two_assays()
        draft_process_chain(state, first, chain=_chain("30 minutes", "counts"))
        draft_process_chain(state, second, chain=_chain("24 hours", "luminescence"))
        graph = assemble_crate(state, output_dir=None, materialize_payload=False)
        nodes = graph.metadata.generate()["@graph"]
        by_id = {node["@id"]: node for node in nodes}

        def listed(value: Any) -> list[str]:
            items = value if isinstance(value, list) else [] if value is None else [value]
            return [item["@id"] for item in items if isinstance(item, dict)]

        def steps_under(assay_name: str) -> list[str]:
            (assay,) = [n for n in nodes if n.get("name") == assay_name]
            held = [by_id[i] for key in ("about", "hasPart") for i in listed(assay.get(key))]
            return sorted(n["name"] for n in held if "LabProcess" in str(n.get("@type")))

        assert steps_under(UPTAKE) == ["Cell culture", "Exposure", "Readout"]
        assert steps_under(VIABILITY) == ["Cell culture", "Exposure", "Readout"]
