"""Each part of a split culture yields cells of the line it grows, in every process.

A TestSystemPreparation naming several cell lines is built as one step per line
(#678), and one part takes the output Sample ``draft_process_chain`` drafted for
the whole culture. That Sample derived from the input ``_input_ref`` drew first
out of a set, so which line it named hung on string hashing: for a culture of
[SK-N-AS, H4] it was SK-N-AS under ``PYTHONHASHSEED=1`` and H4 under 2, 3 and 4.
The part that took it was always the first, so the crate could say the SK-N-AS
culture yielded H4 cells, name the exposed sample after the wrong line, and come
out different for one deposit in two processes.

The drafted Sample derives from the first line as stated, and the part growing
the line it derives from takes it. That part need not be the first, so the
Exposure gives a drafted exposed Sample to the cultured Sample it was drafted
for rather than to the first. A hash seed belongs to a process, so what must
hold across processes is checked in subprocesses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.assessment_graph import with_kinds
from builder.tools.builder import assemble_crate
from builder.tools.composites import draft_process_chain
from builder.tools.drafters import draft_sample

# Every S-VHPS22 process runs the whole pipeline, SHACL passes included.
pytestmark = pytest.mark.timeout(240)

REPO = Path(__file__).resolve().parents[1]
SVHPS22 = REPO / "tests" / "fixtures" / "svhps22_real_input"
# The lines S-VHPS22 cultures, in the order its README names them.
SVHPS22_LINES = ("H4", "MO3.13", "SK-N-AS")
# Under the first seed a set of the two lines' ids yields SK-N-AS first, under the
# second H4: both orders, and two processes.
SEEDS = ("1", "2")
LINES = ["cell_sk_n_as", "cell_h4"]


def _ent(entity_id: str, type_: EntityType, **fields: Any) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _ids(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v.get("@id")) if isinstance(v, dict) else str(v) for v in items]


def _two_lines() -> CrateState:
    """An assay and two lines its culture grows."""
    state = CrateState()
    state.add_entity(_ent("assay_1", "Assay", name="Thyroid hormone uptake"))
    state.add_entity(_ent("cell_sk_n_as", "CellLineSample", name="SK-N-AS"))
    state.add_entity(_ent("cell_h4", "CellLineSample", name="H4"))
    return state


def _culture(lines: list[str], name: str = "Cell culture") -> dict[str, Any]:
    """A culture step for ``draft_process_chain`` naming *lines*, in that order."""
    return {
        "process_type": "TestSystemPreparation",
        "hints": {"name": name, "cell_line": lines},
    }


def _cultures(state: CrateState) -> list[dict[str, Any]]:
    """Each culture step of the crate built from *state*: the lines it grows, and
    each Sample it yields with the lines that Sample derives from, by name."""
    crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    graph = crate.metadata.generate()["@graph"]
    by_id = {node["@id"]: node for node in graph}

    def names(value: Any) -> list[str]:
        return sorted(str(by_id.get(ref, {}).get("name") or ref) for ref in _ids(value))

    cultures = [
        {
            "step": str(node.get("name")),
            "grows": names(node.get("input")),
            "yields": [
                {
                    "sample": ref,
                    "name": by_id.get(ref, {}).get("name"),
                    "derives_from": names(by_id.get(ref, {}).get("derivesFrom")),
                }
                for ref in _ids(node.get("output"))
            ],
        }
        for node in with_kinds(graph)
        if node.get("additionalType") == "TestSystemPreparation"
    ]
    return sorted(cultures, key=lambda culture: culture["step"])


def _exposed(state: CrateState) -> list[dict[str, Any]]:
    """Each Sample the Exposure of the crate built from *state* yields, with the lines
    of the cultured Samples it derives from, by name."""
    crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    graph = crate.metadata.generate()["@graph"]
    by_id = {node["@id"]: node for node in graph}

    def parents(ref: str) -> list[str]:
        return _ids(by_id.get(ref, {}).get("derivesFrom"))

    (exposure,) = (node for node in with_kinds(graph) if node.get("additionalType") == "Exposure")
    return [
        {
            "name": by_id[ref].get("name"),
            "lines": sorted(
                str(by_id.get(line, {}).get("name") or line)
                for cultured in parents(ref)
                for line in parents(cultured)
            ),
        }
        for ref in _ids(exposure.get("output"))
        if "Sample" in _ids(by_id.get(ref, {}).get("@type"))
    ]


def _two_line_culture() -> list[dict[str, Any]]:
    """The cultures built from a culture ``draft_process_chain`` drafted naming two lines."""
    state = _two_lines()
    draft_process_chain(state, "assay_1", [_culture(LINES)])
    return _cultures(state)


def _svhps22_cultures() -> list[dict[str, Any]]:
    """The cultures of S-VHPS22 built as ``tests/test_pipeline_real_input.py`` builds
    a deposit: scanned off disk and run through ``run_pipeline`` offline. What that
    module and ``tests/conftest.py`` stub is stubbed here as well, because their
    stubs do not reach a process of its own. The plan names each line the deposit
    names.

    The pipeline grows only the first line it resolves, so its own culture never
    splits. The deposit is then given the culture an agent drafts for it: one step
    naming every line resolved, in the order the README names them."""
    import builder.agents.pipeline.pipeline as pipeline
    from builder.engine import AgentEngine
    from builder.tools import composites, file_descriptions, lookups
    from builder.tools.hitl import SimulatedHumanInterface

    def plan(context: str, **_: Any) -> dict[str, Any]:
        low = context.lower()
        return {"cell_lines": [{"name": line} for line in SVHPS22_LINES if line.lower() in low]}

    miss = {"found": False, "data": {}, "error": "offline"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pipeline, "get_provider", lambda: "openai")
        patch.setattr(pipeline, "extract_plan", plan)
        patch.setattr(
            pipeline,
            "draft_entity_fields",
            lambda entity_type, context, **_: {
                "description": f"Drafted {entity_type} description."
            },
        )
        patch.setattr(file_descriptions, "describe_payload_files", lambda state, **_: [])
        patch.setattr(lookups, "lookup_compound_wiki", lambda name: {})
        for lookup in (
            "lookup_compound",
            "lookup_dtxsid",
            "lookup_cell_line_by_name",
            "lookup_cell_line",
        ):
            patch.setattr(composites, lookup, lambda query: miss)
        patch.setattr(composites, "lookup_exact_term", lambda label, ontology: miss)
        patch.setattr(composites, "compound_wiki_id", lambda inchikey: "")
        patch.setattr(composites, "search_works_by_title", lambda title: [])
        patch.setattr(
            composites,
            "verify_identifier",
            lambda state, entity_id, field: {"verified": False, "message": "offline"},
        )
        engine = AgentEngine(state=CrateState(), human_interface=SimulatedHumanInterface())
        engine.initialize(input_path=str(SVHPS22))
        pipeline.run_pipeline(engine)
    state = engine.state
    lines = {str(e.fields.get("name")): e.entity_id for e in state.list_entities("CellLineSample")}
    assay = state.list_entities("Assay")[0].entity_id
    # Named apart from the pipeline's "Cell culture", whose step and Sample it would reuse.
    culture = _culture([lines[line] for line in SVHPS22_LINES], "Neural cell line culture")
    draft_process_chain(state, assay, [culture])
    return _cultures(state)


def _in_processes(function: str) -> dict[str, Any]:
    """What *function* of this module returns in a fresh interpreter under each seed."""
    script = (
        f"import json; from tests.{Path(__file__).stem} import {function}; "
        f"print(json.dumps({function}()))"
    )
    runs: dict[str, Any] = {}
    for seed in SEEDS:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO,
            env={**os.environ, "PYTHONHASHSEED": seed, "VITRO_VALIDATE_SERIAL": "1"},
            capture_output=True,
            text=True,
            timeout=200,
        )
        assert proc.returncode == 0, f"PYTHONHASHSEED={seed}:\n{proc.stderr[-2000:]}"
        runs[seed] = json.loads(proc.stdout.strip().splitlines()[-1])
    return runs


def _assert_each_part_yields_its_own_line(cultures: list[dict[str, Any]]) -> None:
    for culture in cultures:
        for sample in culture["yields"]:
            assert sample["derives_from"] == culture["grows"], (
                f"{culture['step']!r} grows {culture['grows']} but yields "
                f"{sample['name']!r}, which derives from {sample['derives_from']}"
            )


class TestEveryProcessBuildsTheSameCultures:
    def test_each_part_of_a_two_line_culture_yields_cells_of_its_own_line(self) -> None:
        runs = _in_processes("_two_line_culture")
        for seed, cultures in runs.items():
            assert [culture["grows"] for culture in cultures] == [["H4"], ["SK-N-AS"]], seed
            _assert_each_part_yields_its_own_line(cultures)
        assert runs[SEEDS[0]] == runs[SEEDS[1]], "one culture built differently in two processes"

    def test_the_svhps22_deposit_gives_the_same_culture_samples_in_two_processes(self) -> None:
        """The deposit whose culture documents the split follows (one per line)."""
        runs = _in_processes("_svhps22_cultures")
        parts = [c["step"] for c in runs[SEEDS[0]] if c["step"].startswith("Culture ")]
        assert parts == [f"Culture {line}" for line in SVHPS22_LINES], runs[SEEDS[0]]
        assert runs[SEEDS[0]] == runs[SEEDS[1]], (
            "S-VHPS22 built different cultures in two processes"
        )
        _assert_each_part_yields_its_own_line(runs[SEEDS[0]])


class TestTheDraftedSampleDerivesFromTheFirstLineStated:
    @pytest.mark.parametrize("lines", [LINES, LINES[::-1]], ids=["sk-n-as-first", "h4-first"])
    def test_whichever_order_the_lines_are_stated_in(self, lines: list[str]) -> None:
        state = _two_lines()
        (sample_id,) = draft_process_chain(state, "assay_1", [_culture(lines)])["synthesized"]
        sample = state.get_entity(sample_id)
        assert sample is not None
        assert sample.fields.get("derives_from") == lines[0]


def _drafted_culture(derives_from: str | None) -> CrateState:
    """A culture of both lines whose drafted output Sample derives from *derives_from*."""
    state = _two_lines()
    lineage = {"derives_from": derives_from} if derives_from else {}
    state.add_entity(_ent("sample_cultured", "Sample", name="Cultured cells", **lineage))
    state.add_entity(
        _ent(
            "proc_culture",
            "LabProcess",
            name="Cell culture",
            process_type="TestSystemPreparation",
            assay_id="assay_1",
            cell_line=LINES,
            result="sample_cultured",
        )
    )
    return state


class TestThePartGrowingItsLineTakesTheDraftedSample:
    """Forced each way round, rather than left to a hash seed."""

    @pytest.mark.parametrize(
        ("derives_from", "taker"),
        [
            ("cell_sk_n_as", "Culture SK-N-AS"),
            ("cell_h4", "Culture H4"),
            # From none of the lines: the first part takes it, and gives it its line.
            (None, "Culture SK-N-AS"),
        ],
    )
    def test_the_part_growing_the_line_it_derives_from_takes_it(
        self, derives_from: str | None, taker: str
    ) -> None:
        cultures = _cultures(_drafted_culture(derives_from))
        yields = {culture["step"]: [s["name"] for s in culture["yields"]] for culture in cultures}
        assert yields[taker] == ["Cultured cells"], yields
        assert sum(names == ["Cultured cells"] for names in yields.values()) == 1, yields
        _assert_each_part_yields_its_own_line(cultures)

    def test_one_deriving_from_a_line_the_culture_does_not_grow_goes_to_the_first(self) -> None:
        """Derived from none of the lines the culture grows, as from no line at all."""
        state = _drafted_culture("cell_mo313")
        state.add_entity(_ent("cell_mo313", "CellLineSample", name="MO3.13"))
        cultures = _cultures(state)
        yields = {culture["step"]: [s["name"] for s in culture["yields"]] for culture in cultures}
        assert yields == {"Culture H4": ["Cultured (H4)"], "Culture SK-N-AS": ["Cultured cells"]}


class TestTheExposedSampleStaysWithItsLine:
    """The drafted cultured Sample derives from H4, so the second part takes it. An
    Exposure pairing its drafted exposed Sample by position gave that Sample to the
    SK-N-AS culture, or exposed H4 cells twice and SK-N-AS cells never."""

    @pytest.mark.parametrize("states_its_lineage", [True, False], ids=["stated", "unstated"])
    def test_the_drafted_exposed_sample_is_of_the_cells_it_was_drafted_for(
        self, states_its_lineage: bool
    ) -> None:
        state = _two_lines()
        cultured = draft_sample(state, {"name": "Cultured H4 cells", "derives_from": "cell_h4"})
        hints = {"name": "H4 cells exposed to T3"}
        if states_its_lineage:
            hints["derives_from"] = cultured.entity_id
        exposed = draft_sample(state, hints)
        exposure = {"process_type": "Exposure", "hints": {"name": "T3 exposure"}}
        draft_process_chain(
            state,
            "assay_1",
            [
                {**_culture(LINES), "result": [cultured.entity_id]},
                {**exposure, "result": [exposed.entity_id]},
            ],
        )
        samples = _exposed(state)
        assert sorted(sample["lines"] for sample in samples) == [["H4"], ["SK-N-AS"]], samples
        (drafted,) = (sample for sample in samples if sample["name"] == "H4 cells exposed to T3")
        assert drafted["lines"] == ["H4"], samples
