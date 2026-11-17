"""An assay belongs to the study it was drafted for.

Two test systems are two studies, and each can have an assay called "Cell
viability". An assay's entity was named after the assay's name alone, so the
second study's assay was the first study's: drafting it replaced the first and
moved it, with every step it had, to the second study. The first study was left
without an assay.

Two studies may name an assay alike. Each keeps its own.
"""

from __future__ import annotations

from typing import Any

from builder.state import CrateState, Entity
from builder.tools.builder import assemble_crate
from builder.tools.composites import draft_process_chain, scaffold_isa_backbone
from builder.tools.drafters import draft_assay, draft_study

TUBULE = "Proximal tubule cells"
ENDOTHELIUM = "Endothelial cells"
VIABILITY = "Cell viability"


def _two_studies() -> tuple[CrateState, str, str]:
    state = CrateState()
    state.metadata.title = "Two test systems"
    made = scaffold_isa_backbone(state, study={"name": TUBULE}, assay={"name": "Transport"})
    other = draft_study(state, made["investigation_id"], {"name": ENDOTHELIUM})
    return state, made["study_id"], other.entity_id


def _assays_of(state: CrateState, study_id: str, name: str = VIABILITY) -> list[Entity]:
    return [
        assay
        for assay in state.list_entities("Assay")
        if assay.fields.get("study_id") == study_id and assay.fields.get("name") == name
    ]


class TestTwoStudiesThatNameAnAssayAlike:
    def test_each_has_its_own_assay(self) -> None:
        state, first, second = _two_studies()
        one = draft_assay(state, first, {"name": VIABILITY})
        two = draft_assay(state, second, {"name": VIABILITY})
        assert one.entity_id != two.entity_id
        assert [a.entity_id for a in _assays_of(state, first)] == [one.entity_id]
        assert [a.entity_id for a in _assays_of(state, second)] == [two.entity_id]

    def test_the_first_keeps_what_its_assay_stated(self) -> None:
        state, first, second = _two_studies()
        draft_assay(state, first, {"name": VIABILITY, "description": "ATP content"})
        draft_assay(state, second, {"name": VIABILITY, "description": "LDH release"})
        (kept,) = _assays_of(state, first)
        assert kept.fields["description"] == "ATP content"

    def test_drafting_either_again_adds_nothing(self) -> None:
        state, first, second = _two_studies()
        one = draft_assay(state, first, {"name": VIABILITY})
        two = draft_assay(state, second, {"name": VIABILITY})
        assert draft_assay(state, second, {"name": VIABILITY}).entity_id == two.entity_id
        assert draft_assay(state, first, {"name": VIABILITY}).entity_id == one.entity_id
        assert len(state.list_entities("Assay")) == 3

    def test_each_assay_keeps_its_steps(self) -> None:
        state, first, second = _two_studies()
        one = draft_assay(state, first, {"name": VIABILITY})
        chain = [
            {"process_type": "Exposure", "hints": {"name": "Exposure", "duration": "24 hours"}}
        ]
        draft_process_chain(state, one.entity_id, chain=chain)
        two = draft_assay(state, second, {"name": VIABILITY})
        draft_process_chain(state, two.entity_id, chain=chain)
        held = {
            assay.entity_id: [
                step.entity_id
                for step in state.list_entities("LabProcess")
                if step.fields.get("assay_id") == assay.entity_id
            ]
            for assay in (one, two)
        }
        assert all(len(steps) == 1 for steps in held.values()), held
        assert held[one.entity_id] != held[two.entity_id]


class TestWhatDoesNotChange:
    def test_an_assay_keeps_the_identifier_its_name_gives(self) -> None:
        state, first, _ = _two_studies()
        assert draft_assay(state, first, {"name": VIABILITY}).entity_id == "assay_cell_viability"

    def test_one_study_drafting_an_assay_again_has_one_assay(self) -> None:
        state, first, _ = _two_studies()
        draft_assay(state, first, {"name": VIABILITY, "description": "ATP content"})
        draft_assay(state, first, {"name": VIABILITY, "description": "ATP content, luminescence"})
        (assay,) = _assays_of(state, first)
        assert assay.fields["description"] == "ATP content, luminescence"

    def test_an_identifier_the_caller_gives_names_that_assay(self) -> None:
        state, first, second = _two_studies()
        draft_assay(state, first, {"name": VIABILITY, "entity_id": "assay_shared"})
        draft_assay(state, second, {"name": VIABILITY, "entity_id": "assay_shared"})
        assert [
            a.entity_id for a in state.list_entities("Assay") if a.fields["name"] == VIABILITY
        ] == ["assay_shared"]


class TestTheCrate:
    def test_each_study_holds_its_own_assay(self) -> None:
        state, first, second = _two_studies()
        draft_assay(state, first, {"name": VIABILITY})
        draft_assay(state, second, {"name": VIABILITY})
        nodes = assemble_crate(state, output_dir=None, materialize_payload=False)
        graph = nodes.metadata.generate()["@graph"]
        by_id = {node["@id"]: node for node in graph}

        def listed(value: Any) -> list[str]:
            items = value if isinstance(value, list) else [] if value is None else [value]
            return [item["@id"] for item in items if isinstance(item, dict)]

        def assays_under(study_name: str) -> list[str]:
            (study,) = [n for n in graph if n.get("name") == study_name]
            held = [by_id[i] for i in listed(study.get("hasPart")) if i in by_id]
            return sorted(n["name"] for n in held if n.get("additionalType") == "Assay")

        assert assays_under(TUBULE) == sorted([VIABILITY, "Transport"])
        assert assays_under(ENDOTHELIUM) == [VIABILITY]
