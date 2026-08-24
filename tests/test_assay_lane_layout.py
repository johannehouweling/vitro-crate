"""Where the lane puts an assay's nodes (#686).

The layout is JavaScript, and it is the shipped file these tests run: a node
probe (``tests/fixtures/assay_lane_probe.js``) requires
``builder/writers/assay_lane_layout.js`` and prints the positions it computed,
so what is measured here is the code the report carries.

The defect: the LabProcesses view draws 74 nodes for 15 steps, and a layered
layout gives a protocol its own rank to the *right* of the step that executes
it, so the material chain a reader is trying to follow is interrupted by things
that are not material at all. The lane separates the two directions —
**horizontal is the material chain, vertical is what qualifies a step** — so the
left-to-right reading is never displaced by a protocol, a compound, or how many
of either there are.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from builder.writers.entity_explorer import build_explorer_payload
from tests.fixtures.crate_graphs import assay_lane_graph

pytestmark = pytest.mark.timeout(180)

_REPO = Path(__file__).resolve().parents[1]
_MODULE = _REPO / "builder" / "writers" / "assay_lane_layout.js"
_PROBE = _REPO / "tests" / "fixtures" / "assay_lane_probe.js"


def _node_exe() -> str:
    exe = shutil.which("node")
    if exe is None:  # pragma: no cover — depends on the machine, not the code
        pytest.skip("node is not installed; the layout module cannot be run")
    return exe


def _run(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    result = subprocess.run(
        [_node_exe(), str(_PROBE), str(_MODULE)],
        input=json.dumps({"nodes": nodes, "edges": edges}),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _lane_input(graph: dict[str, Any], key: str) -> tuple[list[dict], list[dict]]:
    """A lane's members and the edges among them, as the app would hand them over.

    Mirrors ``visibleGraph``: one edge per pair, carrying every relation that
    connects them.
    """
    payload = build_explorer_payload(graph)
    view = next(v for v in payload["views"] if v["key"] == key)
    visible = set(view["members"])
    by_id = {n["id"]: n for n in payload["nodes"]}
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in payload["edges"]:
        pair = (edge["src"], edge["dst"])
        if pair[0] == pair[1] or not visible >= set(pair):
            continue
        entry = merged.setdefault(pair, {"src": pair[0], "dst": pair[1], "labels": []})
        if edge["label"] not in entry["labels"]:
            entry["labels"].append(edge["label"])
    nodes = [
        {"id": i, "category": by_id[i]["category"], "type": by_id[i]["type"]}
        for i in sorted(visible)
    ]
    return nodes, list(merged.values())


@pytest.fixture(scope="module")
def lane() -> dict[str, Any]:
    nodes, edges = _lane_input(assay_lane_graph(), "assay-deiodinase-assay")
    out = _run(nodes, edges)
    assert out["positions"] is not None, "the lane declined a graph that is a lane"
    return out["positions"]


def _x(lane, i):
    return lane[i]["x"]


def _y(lane, i):
    return lane[i]["y"]


SPINE = [
    "#cellline-a",
    "#culture-a",
    "#cultured-a",
    "#exposure-a",
    "#exposed-a",
    "#readout-a",
    "#analysis-a",
    "processed/a.csv",
]
BAND = [
    "#culture-protocol-a",
    "#conditions-a",
    "#readout-protocol-a",
    "#analysis-protocol-a",
]
COMPOUNDS = ["#compound-a1", "#compound-a2"]


class TestTheMaterialChainReadsLeftToRight:
    def test_every_step_is_right_of_the_one_before_it(self, lane):
        xs = [(i, _x(lane, i)) for i in SPINE]
        for (before, bx), (after, ax) in zip(xs, xs[1:]):
            assert bx < ax, f"{after} is not right of {before}: {bx} vs {ax}"

    def test_the_cell_line_opens_the_spine(self, lane):
        """Nothing precedes a cell line, so nothing crosses it."""
        assert _x(lane, "#cellline-a") == min(lane[i]["x"] for i in lane)

    def test_the_raw_files_sit_between_the_readout_and_the_analysis(self, lane):
        """Raw data is on the material spine; the edge into the analysis has to
        stay horizontal, which is why a file stack never drops to the band."""
        for raw in ("raw/a1.csv", "raw/a2.csv"):
            assert _x(lane, "#readout-a") < _x(lane, raw) < _x(lane, "#analysis-a"), raw


class TestWhatQualifiesAStepHangsBelowIt:
    def test_every_protocol_is_below_every_step_it_could_belong_to(self, lane):
        """The band is a tier, not an interleaving: a reader scanning the spine
        never has their eye caught by something that is not material."""
        floor = max(_y(lane, i) for i in SPINE)
        for protocol in BAND:
            assert _y(lane, protocol) > floor, protocol

    def test_a_protocol_sits_under_the_step_that_executes_it(self, lane):
        """Which is what makes the attachment unambiguous without a label."""
        for step, protocol in (
            ("#culture-a", "#culture-protocol-a"),
            ("#exposure-a", "#conditions-a"),
            ("#readout-a", "#readout-protocol-a"),
            ("#analysis-a", "#analysis-protocol-a"),
        ):
            assert abs(_x(lane, step) - _x(lane, protocol)) < 1, (
                f"{protocol} is not under {step}: {_x(lane, protocol)} vs {_x(lane, step)}"
            )

    def test_several_protocols_under_one_step_do_not_stack(self):
        """A readout that isolates RNA, makes cDNA and runs qPCR executes three
        protocols, and the real crate has exactly that. Anchoring each to its
        step's x put all three at one point, which draws as one node and hides
        the other two entirely.
        """
        graph = assay_lane_graph()
        extra = [
            {"@id": f"#readout-extra-{i}", "@type": "LabProtocol", "name": f"Step {i}"}
            for i in range(2)
        ]
        for entity in graph["@graph"]:
            if entity.get("@id") == "#readout-a":
                entity["executesLabProtocol"] = [
                    {"@id": "#readout-protocol-a"},
                    *({"@id": e["@id"]} for e in extra),
                ]
        graph["@graph"].extend(extra)
        lane = _lane_of(graph)

        placed = [
            (lane[i]["x"], lane[i]["y"])
            for i in ("#readout-protocol-a", "#readout-extra-0", "#readout-extra-1")
        ]
        assert len(set(placed)) == 3, f"two protocols share a point: {placed}"

    def test_the_compounds_hang_off_the_condition_table(self, lane):
        """Not off the spine: twelve of them cost one rank of height and no
        horizontal travel."""
        for compound in COMPOUNDS:
            assert _y(lane, compound) > _y(lane, "#conditions-a"), compound

    def test_a_compound_never_displaces_the_spine(self, lane):
        """The property the whole two-band split exists to buy."""
        spine_right = max(_x(lane, i) for i in SPINE)
        assert all(_x(lane, c) <= spine_right for c in COMPOUNDS)


def _with_compounds(count: int) -> dict[str, Any]:
    """The lane fixture, with *count* compounds on the exposure's table.

    A real condition table lists a dozen; the fixture's two are enough to place
    them and not enough to show what placing them costs.
    """
    graph = assay_lane_graph()
    extra = [
        {"@id": f"#compound-x{i}", "@type": "MolecularEntity", "name": f"Compound {i}"}
        for i in range(count)
    ]
    for entity in graph["@graph"]:
        if entity.get("@id") == "#conditions-a":
            entity["reagent"] = [{"@id": c["@id"]} for c in extra]
    graph["@graph"].extend(extra)
    return graph


def _lane_of(graph: dict[str, Any]) -> dict[str, Any]:
    nodes, edges = _lane_input(graph, "assay-deiodinase-assay")
    out = _run(nodes, edges)
    assert out["positions"] is not None
    return out["positions"]


def _generic(nodes: list[dict], edges: list[dict]) -> dict[str, Any]:
    """The same graph through the shipped generic layout, for comparison."""
    result = subprocess.run(
        [
            _node_exe(),
            str(_REPO / "tests" / "fixtures" / "layout_probe.js"),
            str(_REPO / "builder" / "writers" / "entity_explorer_layout.js"),
        ],
        input=json.dumps({"nodes": [n["id"] for n in nodes], "edges": edges}),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["positions"]


def _width(positions: dict[str, Any]) -> float:
    xs = [p["x"] for p in positions.values()]
    return max(xs) - min(xs)


def _height(positions: dict[str, Any]) -> float:
    ys = [p["y"] for p in positions.values()]
    return max(ys) - min(ys)


class TestTheBandNeverWidensTheChain:
    """The property the two-band split exists to buy, stated as an invariant
    rather than as one measurement of one crate.

    A dependency-ranked layout gives every compound a rank of its own to the
    right, so the chain a reader is following gets longer the more substances
    the exposure used — which is exactly backwards.
    """

    def test_the_spine_does_not_move_when_compounds_are_added(self):
        few, many = _lane_of(_with_compounds(2)), _lane_of(_with_compounds(12))
        for step in SPINE:
            assert few[step] == many[step], (
                f"{step} moved when compounds it has nothing to do with were added: "
                f"{few[step]} -> {many[step]}"
            )

    def test_twelve_compounds_cost_height_and_not_width(self):
        few, many = _lane_of(_with_compounds(2)), _lane_of(_with_compounds(12))
        assert _width(many) <= _width(few), (_width(few), _width(many))
        assert _height(many) > _height(few), "they have to go somewhere"

    def test_the_lane_is_no_wider_than_the_generic_canvas_and_far_shorter(self):
        """The comparison that justifies a second layout module existing.

        Width ties here, and that is the honest result rather than a weaker
        claim: both layouts are as wide as the chain itself, because the lane
        bounds its band by the spine's width and the generic canvas has the same
        chain to draw. What the generic canvas spends instead is height — it
        gives each of the twelve compounds a row, under a protocol that is
        already a rank of its own.
        """
        nodes, edges = _lane_input(_with_compounds(12), "assay-deiodinase-assay")
        lane = _run(nodes, edges)["positions"]
        generic = _generic(nodes, edges)
        assert _width(lane) <= _width(generic), (_width(lane), _width(generic))
        assert _height(lane) * 2 < _height(generic), (
            f"lane {_width(lane)}x{_height(lane)} vs generic {_width(generic)}x{_height(generic)}"
        )


class TestWhatTheLaneDeclines:
    """An assay that does not fit the spine is drawn by the generic canvas, same
    styling, no visible seam. Declining is the module's job, not the caller's."""

    def test_a_graph_with_no_process_is_declined(self):
        nodes = [
            {"id": "a", "category": "data", "type": "File"},
            {"id": "b", "category": "data", "type": "File"},
        ]
        out = _run(nodes, [{"src": "a", "dst": "b", "labels": ["hasPart"]}])
        assert out["positions"] is None

    def test_two_disjoint_chains_are_declined(self):
        """The whole-crate LabProcesses view is several assays at once, and a
        lane is one. Laying that out as a spine would interleave two stories."""
        nodes, edges = _lane_input(assay_lane_graph(), "processes")
        out = _run(nodes, edges)
        assert out["positions"] is None

    def test_an_empty_graph_is_declined(self):
        assert _run([], [])["positions"] is None
