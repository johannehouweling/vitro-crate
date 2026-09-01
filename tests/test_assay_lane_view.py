"""What the assay lane draws, measured on the shapes a real deposit has (#686).

The lane view shipped with 43 tests and four defects, because every one of them
drove ``assay_lane_graph`` — a fixture that predates #678 and gives each assay a
single cell line. A real deposit cultures each line separately, so a rank holds
several steps, and it runs characterisation assays with no exposure at all.
Neither shape was ever laid out in CI.

These tests drive ``assay_lane_real_shapes_graph`` instead, and they run the
SHIPPED JavaScript over it rather than a Python restatement of the geometry —
the report carries the module, so the module is what has to be measured.

The lane is its own view rather than a re-ranking of the generic canvas
(``assay_lane_view.js``). A step's place comes from what it IS in the ISA-Tox
chain, not from where a layered pass happens to put it, which is why two steps
of one kind cannot collide and why an assay missing a kind still draws.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from builder.writers.entity_explorer import build_explorer_payload
from tests.fixtures.crate_graphs import assay_lane_real_shapes_graph

pytestmark = pytest.mark.timeout(180)

_REPO = Path(__file__).resolve().parents[1]
_MODULE = _REPO / "builder" / "writers" / "assay_lane_view.js"
_PROBE = _REPO / "tests" / "fixtures" / "assay_lane_view_probe.js"


def _node_exe() -> str:
    exe = shutil.which("node")
    if exe is None:  # pragma: no cover — depends on the machine, not the code
        pytest.skip("node is not installed; the lane module cannot be run")
    return exe


def _lanes() -> dict[str, dict[str, Any]]:
    """Every lane the fixture mints, drawn by the shipped module.

    One payload build and one node process per lane, keyed by lane — the tests
    below each ask a different question of the same drawing.
    """
    payload = build_explorer_payload(assay_lane_real_shapes_graph())
    by_id = {n["id"]: n for n in payload["nodes"]}
    out: dict[str, dict[str, Any]] = {}
    for lane in payload["lanes"]:
        members = set(lane["members"])
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for edge in payload["edges"]:
            if edge["src"] in members and edge["dst"] in members:
                key = (edge["src"], edge["dst"])
                merged.setdefault(key, {"src": key[0], "dst": key[1], "labels": []})
                merged[key]["labels"].append(edge["label"])
        nodes = [
            {"id": i, "category": by_id[i]["category"], "type": by_id[i].get("type")}
            for i in sorted(members)
        ]
        result = subprocess.run(
            [_node_exe(), str(_PROBE), str(_MODULE)],
            input=json.dumps(
                {
                    "nodes": nodes,
                    "edges": list(merged.values()),
                    # Which way each relation is really stated — from the crate's
                    # own relation tables, never a literal in the browser.
                    "reversed": payload["relations_reversed"],
                }
            ),
            capture_output=True,
            text=True,
            check=True,
        )
        out[lane["key"]] = json.loads(result.stdout)
    return out


@pytest.fixture(scope="module")
def lanes() -> dict[str, dict[str, Any]]:
    return _lanes()


def _row_pitch(drawing: dict[str, Any]) -> float:
    """How far apart two boxes stacked in one column sit — box plus gap.

    Measured on the drawing rather than restated from the module's constants: a
    test that repeats them passes when they change and proves nothing about the
    rule they serve.
    """
    column = next(r["members"] for r in drawing["ranks"] if len(r["members"]) > 1)
    tops = sorted(drawing["positions"][i]["y"] for i in column)
    return tops[1] - tops[0]


def _boxes(drawing: dict[str, Any]) -> dict[str, tuple[float, float, float, float]]:
    return {
        i: (p["x"], p["y"], p["x"] + p["w"], p["y"] + p["h"])
        for i, p in drawing["positions"].items()
    }


class TestNothingIsDrawnUnderneathSomethingElse:
    """The defect: three protocols at one point, and two of them invisible.

    ``assay_lane_layout`` gave a step's satellites the step's own x and every
    group one shared top, so sibling steps in a rank — which is what #678 made
    normal — dealt their protocols into the same cell. A reader saw one node and
    had no way to know two more were behind it.
    """

    def test_no_two_nodes_share_a_point(self, lanes):
        for key, drawing in lanes.items():
            seen: dict[tuple[float, float], str] = {}
            for node_id, pos in sorted(drawing["positions"].items()):
                at = (pos["x"], pos["y"])
                assert at not in seen, f"{key}: {node_id} is drawn on top of {seen[at]} at {at}"
                seen[at] = node_id

    def test_no_two_nodes_overlap_at_all(self, lanes):
        """Stronger than distinct corners: a partial overlap hides text too."""
        for key, drawing in lanes.items():
            boxes = _boxes(drawing)
            for (a, ab), (b, bb) in combinations(sorted(boxes.items()), 2):
                apart = ab[2] <= bb[0] or bb[2] <= ab[0] or ab[3] <= bb[1] or bb[3] <= ab[1]
                assert apart, f"{key}: {a} {ab} overlaps {b} {bb}"


class TestAnAssayIsDrawnEvenWhereItIsIncomplete:
    """A characterisation run measures cultured material with no exposure.

    The old module required the spine to be one connected component and returned
    ``null`` otherwise, so the caller fell back to the generic canvas. But the
    reason a lane matters most is exactly when a step is missing: the empty rank
    is the finding. Declining hid the work AND the gap.
    """

    def test_an_exposure_free_assay_still_draws(self, lanes):
        drawing = lanes["assay-characterisation-assay"]
        assert drawing["positions"], "an assay with no Exposure was declined"

    def test_the_missing_step_is_named_as_missing(self, lanes):
        """The rank stays in the sequence, and says nothing was recorded for it."""
        drawing = lanes["assay-characterisation-assay"]
        empty = [r["key"] for r in drawing["ranks"] if not r["members"]]
        # The fixture's characterisation run cultures, measures, and stops: no
        # exposure and no analysis. Every step it did not run keeps its column.
        assert empty == ["exposure", "exposed", "analysis", "processed"], drawing["ranks"]

    def test_a_complete_assay_has_no_empty_rank(self, lanes):
        drawing = lanes["assay-transport-assay"]
        assert [r["key"] for r in drawing["ranks"] if not r["members"]] == []


class TestAStepIsPlacedByWhatItIs:
    """Rank comes from the ISA-Tox chain, not from a layered pass.

    This is what makes the two defects above unreachable rather than fixed: two
    CellCultures cannot land in one cell because the rank holds a column, and a
    missing Exposure leaves a column empty rather than splitting the graph.
    """

    def test_the_ranks_are_the_chain_in_order(self, lanes):
        expected = [
            "cellline",
            "culture",
            "cultured",
            "exposure",
            "exposed",
            "readout",
            "raw",
            "analysis",
            "processed",
        ]
        for key, drawing in lanes.items():
            assert [r["key"] for r in drawing["ranks"]] == expected, key

    def test_every_line_gets_its_own_culture_step(self, lanes):
        """#678's per-line split, seen from the diagram."""
        drawing = lanes["assay-transport-assay"]
        ranks = {r["key"]: r["members"] for r in drawing["ranks"]}
        assert len(ranks["cellline"]) == 3
        assert len(ranks["culture"]) == 3
        assert len(ranks["cultured"]) == 3

    def test_a_rank_is_a_column(self, lanes):
        """Every member of a rank shares an x; ranks march left to right."""
        for key, drawing in lanes.items():
            last = -1.0
            for rank in drawing["ranks"]:
                xs = {drawing["positions"][i]["x"] for i in rank["members"]}
                assert len(xs) <= 1, f"{key}: rank {rank['key']} is not a column: {xs}"
                if xs:
                    assert xs.pop() > last
                    last = drawing["positions"][rank["members"][0]]["x"]


class TestEveryEdgeReadsWithItsArrow:
    """The defect: a third of lane edges named the predicate backwards.

    The model draws `input` and `reagent` reversed so the arrow points the way
    the material moves — deliberate, and the band depends on it. But the label
    was the un-reversed term, so an arrow from a cell line to the step that
    consumed it read `schema:object`, asserting the exact inverse of the triple
    the crate holds.
    """

    def test_a_reversed_edge_is_marked_as_reversed(self, lanes):
        drawing = lanes["assay-transport-assay"]
        reversed_labels = {e["label"] for e in drawing["edges"] if e["reversed"]}
        assert reversed_labels == {"input", "reagent"}, reversed_labels

    def test_a_forward_edge_is_not(self, lanes):
        drawing = lanes["assay-transport-assay"]
        forward = {e["label"] for e in drawing["edges"] if not e["reversed"]}
        assert "result" in forward
        assert "executes" in forward
        assert not forward & {"input", "reagent"}

    def test_the_subject_of_the_triple_is_named(self, lanes):
        """Not just a flag: the drawing says which end the crate states it from,
        so a renderer cannot get the direction right by accident and wrong on
        the next relation added."""
        drawing = lanes["assay-transport-assay"]
        for edge in drawing["edges"]:
            subject = edge["dst"] if edge["reversed"] else edge["src"]
            assert edge["subject"] == subject, edge


class TestTheLaneEarnsItsPlace:
    """A lane exists to be read left to right in one pass.

    Three real lanes were bigger than the generic layout on BOTH axes, which
    made the view a cost with no benefit. Fixed ranks trade width — nine columns
    is the chain, and that is the point — for a chain that stays flat however
    many lines an assay cultures.

    Edge crossings are deliberately NOT asserted. Measured over all 14 lanes of
    the reference deposit (v26/v27/v28), the chain crosses itself fewer times
    than the generic canvas on 12 and marginally more on 2 (10 vs 9, 55 vs 54).
    A test claiming the lane always wins would pass here and be false there,
    which is the fixture-only failure this whole module exists to stop
    repeating. The band's dashed connectors drop through the chain and add more
    crossings still; folding them to one connector per group at rest is the
    remaining readability work, not something to assert before it is done.
    """

    def test_the_chain_is_never_taller_than_the_generic_canvas(self, lanes):
        """Compared on the half both layouts draw.

        The lane is wider by construction and carries a band the canvas has no
        equivalent for, so a whole-figure comparison would measure the extra
        information rather than the packing.
        """
        for key, drawing in lanes.items():
            assert drawing["bandTop"] <= drawing["generic"]["height"], (
                f"{key}: chain {drawing['bandTop']} vs generic "
                f"{drawing['generic']['height']}"
            )

    def test_the_chain_height_does_not_grow_with_the_number_of_lines(self, lanes):
        """The property the deposit actually needs.

        A layered pass makes the canvas as tall as the widest rank is long, so a
        three-line assay ran to 1,125 px against this module's 344. Here the
        chain is as tall as the busiest rank and nothing else, so the two lanes
        below differ by their line count alone.
        """
        three = lanes["assay-transport-assay"]["bandTop"]
        two = lanes["assay-characterisation-assay"]["bandTop"]
        # One extra line is one extra row, not a re-ranking of the whole graph.
        # The row pitch is read off the drawing rather than written here, so the
        # claim survives a change of node size and fails on a change of rule.
        assert three - two == _row_pitch(lanes["assay-transport-assay"]), (three, two)
