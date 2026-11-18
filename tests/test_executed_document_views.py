"""The report's views of a crate whose steps execute deposited documents.

Where the deposit holds a document for a step, the step executes that file: a
``File`` typed ``LabProtocol`` as well, carrying the ``intendedUse`` that types the
step and the ``reagent`` the step applied. One file can then be the protocol of
several steps, and a step can execute several files. Every other view fixture
gives each step one record of its own, so these tests drive
``executed_document_graph`` through the same readers: the assay lane the report
draws with the shipped JavaScript, the lane's selection, the crate graph and the
compound inventory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from builder.writers.entity_explorer import (
    _Crate,
    _select_assay_lane,
    _select_processes,
    build_explorer_payload,
)
from builder.writers.provenance_dag import build_chemical_inventory, build_crate_graph
from tests.fixtures.crate_graphs import executed_document_graph

pytestmark = pytest.mark.timeout(180)

_REPO = Path(__file__).resolve().parents[1]
_MODULE = _REPO / "builder" / "writers" / "assay_lane_view.js"
_PROBE = _REPO / "tests" / "fixtures" / "assay_lane_view_probe.js"

CULTURE_DOCUMENT = "cell_line_protocols/cell%20culture%20SK-N-AS.docx"
SOP = "assay_e/SOP.docx"
SCRIPT = "assay_e/fit_curves.R"
RECORD = "#exposure-e_protocol"
DOCUMENTS_F = {"assay_f/lysis.docx", "assay_f/counting.docx"}


@pytest.fixture(scope="module")
def crate() -> _Crate:
    return _Crate(executed_document_graph())


@pytest.fixture(scope="module")
def model() -> dict[str, Any]:
    return build_crate_graph(executed_document_graph(), layer="all", all_edges=True)


@pytest.fixture(scope="module")
def lanes() -> dict[str, dict[str, Any]]:
    """Each lane of the fixture, drawn by the module the report ships."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover — depends on the machine, not the code
        pytest.skip("node is not installed; the lane module cannot be run")
    payload = build_explorer_payload(executed_document_graph())
    by_id = {n["id"]: n for n in payload["nodes"]}
    drawn: dict[str, dict[str, Any]] = {}
    for lane in payload["lanes"]:
        members = set(lane["members"])
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for edge in payload["edges"]:
            if edge["src"] in members and edge["dst"] in members:
                key = (edge["src"], edge["dst"])
                merged.setdefault(key, {"src": key[0], "dst": key[1], "labels": []})
                merged[key]["labels"].append(edge["label"])
        result = subprocess.run(
            [node, str(_PROBE), str(_MODULE)],
            input=json.dumps(
                {
                    "nodes": [
                        {"id": i, "category": by_id[i]["category"], "type": by_id[i].get("type")}
                        for i in sorted(members)
                    ],
                    "edges": list(merged.values()),
                    "reversed": payload["relations_reversed"],
                }
            ),
            capture_output=True,
            text=True,
            check=True,
        )
        drawn[lane["key"]] = json.loads(result.stdout)
    return drawn


def _hangs(drawing: dict[str, Any], label: str) -> set[tuple[str, str]]:
    """What hangs below the chain by *label*, as ``(member, anchor)`` pairs."""
    return {(h["id"], h["anchor"]) for h in drawing["band"] if h["label"] == label}


class TestTheLaneHangsAnExecutedDocumentUnderItsSteps:
    def test_a_document_two_steps_execute_hangs_from_each(self, lanes):
        hangs = _hangs(lanes["assay-uptake-assay"], "executes")
        assert {(SOP, "#readout-e1"), (SOP, "#readout-e2")} <= hangs, sorted(hangs)

    def test_it_is_drawn_once_below_the_chain(self, lanes):
        drawing = lanes["assay-uptake-assay"]
        assert drawing["positions"][SOP]["y"] >= drawing["bandTop"]
        readouts = next(r["members"] for r in drawing["ranks"] if r["key"] == "readout")
        assert readouts == ["#readout-e1", "#readout-e2"], "the document is no step of the chain"

    def test_every_step_has_what_it_executes_under_it(self, lanes):
        assert _hangs(lanes["assay-uptake-assay"], "executes") == {
            (CULTURE_DOCUMENT, "#culture-e"),
            (RECORD, "#exposure-e"),
            (SOP, "#readout-e1"),
            (SOP, "#readout-e2"),
            (SCRIPT, "#analysis-e"),
        }

    def test_a_step_that_executes_two_documents_has_both_under_it(self, lanes):
        assert _hangs(lanes["assay-efflux-assay"], "executes") == {
            (CULTURE_DOCUMENT, "#culture-f"),
            *((document, "#readout-f") for document in DOCUMENTS_F),
        }

    def test_a_reagent_hangs_from_the_document_that_lists_it(self, lanes):
        assert _hangs(lanes["assay-uptake-assay"], "reagent") == {
            ("#substrate", SOP),
            ("#compound", RECORD),
        }

    def test_no_two_boxes_overlap(self, lanes):
        for key, drawing in lanes.items():
            boxes = {
                i: (p["x"], p["y"], p["x"] + p["w"], p["y"] + p["h"])
                for i, p in drawing["positions"].items()
            }
            for (a, ab), (b, bb) in combinations(sorted(boxes.items()), 2):
                apart = ab[2] <= bb[0] or bb[2] <= ab[0] or ab[3] <= bb[1] or bb[3] <= ab[1]
                assert apart, f"{key}: {a} {ab} overlaps {b} {bb}"


class TestTheLaneSelectsWhatItsStepsExecute:
    def test_the_lane_carries_the_documents_its_steps_execute(self, crate):
        lane = _select_assay_lane(crate, "#assay-e")
        assert {CULTURE_DOCUMENT, SOP, SCRIPT, RECORD} <= lane, sorted(lane)

    def test_a_compound_listed_on_an_executed_document_is_on_the_lane(self, crate):
        assert {"#substrate", "#compound"} <= _select_assay_lane(crate, "#assay-e")

    def test_a_document_two_assays_execute_is_on_both_lanes(self, crate):
        """The culture document is the one node the two lanes have in common, beside
        the cell line both assays grow."""
        shared = _select_assay_lane(crate, "#assay-e") & _select_assay_lane(crate, "#assay-f")
        assert shared == {CULTURE_DOCUMENT, "#cellline"}

    def test_a_readout_view_draws_the_substrate_on_its_document(self, crate):
        selected = _select_processes(crate, "EndpointReadout")
        assert {SOP, "#substrate"} | DOCUMENTS_F <= selected, sorted(selected)
        assert "#compound" not in selected, "the exposure's compound is no readout's"


class TestTheGraphDrawsAnExecutedDocumentAsAProtocol:
    def test_an_executed_file_is_a_protocol_and_a_result_file_is_data(self, model):
        category = {n["id"]: n["category"] for n in model["nodes"]}
        for document in (CULTURE_DOCUMENT, SOP, SCRIPT, RECORD, *DOCUMENTS_F):
            assert category[document] == "protocol", document
        assert category["raw/e1.csv"] == "data"

    def test_a_step_is_typed_by_the_document_it_executes(self, model):
        kind = {n["id"]: n["type"] for n in model["nodes"] if n["category"] == "process"}
        assert kind == {
            "#culture-e": "TestSystemPreparation",
            "#culture-f": "TestSystemPreparation",
            "#exposure-e": "Exposure",
            "#readout-e1": "EndpointReadout",
            "#readout-e2": "EndpointReadout",
            "#readout-f": "EndpointReadout",
            "#analysis-e": "DataAnalysis",
        }

    def test_every_step_points_at_each_document_it_executes(self, model):
        executes = {(e["src"], e["dst"]) for e in model["edges"] if e["label"] == "executes"}
        assert {("#readout-e1", SOP), ("#readout-e2", SOP)} <= executes
        assert {("#readout-f", document) for document in DOCUMENTS_F} <= executes
        assert {("#culture-e", CULTURE_DOCUMENT), ("#culture-f", CULTURE_DOCUMENT)} <= executes

    def test_a_compound_on_a_shared_document_is_wired_to_a_step_that_executes_it(self):
        """The inventory names one step per compound: the first, by id, of the steps
        that execute the document listing it."""
        inventory = build_chemical_inventory(executed_document_graph())
        routes = {c["id"]: (c["state"], c["route"]) for c in inventory["chemicals"]}
        assert routes["#substrate"] == (
            "wired",
            {"process": "#readout-e1", "via": SOP, "link": SOP, "edge": "reagent"},
        )
        assert routes["#compound"][1]["via"] == RECORD
