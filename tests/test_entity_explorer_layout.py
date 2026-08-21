"""Where the explorer puts the nodes it draws (#619).

The layout is JavaScript, and it is the shipped file these tests run: a node
probe (``tests/fixtures/layout_probe.js``) requires
``builder/writers/entity_explorer_layout.js`` and prints the positions it
computed, so what is measured here is the code the report carries rather than a
Python restatement of it.

The defect these pin: a layered layout gives every node in a rank its own row,
so a root that ``hasPart`` sixty files lays out as a column sixty nodes tall —
12,100 px on a real deposit, inside a 620 px canvas, which is a fit zoom of 0.05
and a field of unreadable slivers. Leaves carry no downstream structure, so
their row says nothing that a grid cell does not.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from builder.writers.entity_explorer import build_explorer_payload
from tests.fixtures.crate_graphs import tabbed_views_graph, wide_fanout_graph

_REPO = Path(__file__).resolve().parents[1]
_MODULE = _REPO / "builder" / "writers" / "entity_explorer_layout.js"
_PROBE = _REPO / "tests" / "fixtures" / "layout_probe.js"

# The report's canvas, in CSS pixels — the box a view has to be legible inside.
CANVAS_W, CANVAS_H = 1200, 620


def _module_constant(name: str) -> float:
    """A number the layout module declares, read from it rather than copied."""
    match = re.search(rf"var {name} = ([\d.]+)", _MODULE.read_text(encoding="utf-8"))
    assert match, f"the layout module no longer declares {name}"
    return float(match.group(1))


def _app_constant(name: str) -> float:
    """A number the shipped explorer declares, read from it rather than copied.

    The threshold these tests measure against is the app's own: a layout that
    cannot be framed above ``FIT_FLOOR`` opens cropped, because that is as far
    as the opening ``fitView`` is allowed to pull back.
    """
    from builder.writers.entity_explorer import _app_js

    match = re.search(rf"var {name} = ([\d.]+);", _app_js())
    assert match, f"the explorer no longer declares {name}"
    return float(match.group(1))


def _node() -> str:
    exe = shutil.which("node")
    if exe is None:  # pragma: no cover — depends on the machine, not the code
        pytest.skip("node is not installed; the layout module cannot be run")
    return exe


def _positions(nodes: list[str], edges: list[dict[str, str]]) -> dict[str, Any]:
    """Run the shipped layout over *nodes*/*edges* and return what it produced."""
    result = subprocess.run(
        [_node(), str(_PROBE), str(_MODULE)],
        input=json.dumps({"nodes": nodes, "edges": edges}),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _canvas(graph: dict[str, Any]) -> dict[str, Any]:
    """Lay out everything a crate's payload can draw.

    Uses the ``all`` view deliberately: its edge rule is "every link the payload
    holds", so the test states the graph rather than re-implementing the app's
    view filtering.
    """
    payload = build_explorer_payload(graph)
    visible = {
        member for view in payload["views"] if view["key"] == "all" for member in view["members"]
    }
    seen, edges = set(), []
    for edge in payload["edges"]:
        pair = (edge["src"], edge["dst"])
        if pair[0] == pair[1] or pair in seen or not visible >= set(pair):
            continue
        seen.add(pair)
        edges.append({"src": pair[0], "dst": pair[1]})
    return _positions(sorted(visible), edges) | {"edges": edges}


def _extent(out: dict[str, Any]) -> tuple[float, float]:
    xs = [p["x"] for p in out["positions"].values()]
    ys = [p["y"] for p in out["positions"].values()]
    return (
        max(xs) + out["nodeW"] - min(xs),
        max(ys) + out["nodeH"] - min(ys),
    )


def _fit(out: dict[str, Any]) -> float:
    """The zoom that frames the whole layout in the report's canvas."""
    width, height = _extent(out)
    return min(CANVAS_W / width, CANVAS_H / height)


def _columns(out: dict[str, Any], ids: list[str]) -> set[float]:
    return {out["positions"][i]["x"] for i in ids}


class TestTheProbeRunsTheShippedModule:
    """If these two go quiet the suite would pass while measuring nothing."""

    def test_the_module_the_probe_loads_is_the_one_the_report_inlines(self) -> None:
        from builder.writers.entity_explorer import _layout_js

        assert _MODULE.read_text(encoding="utf-8") == _layout_js()

    @pytest.mark.skipif(not os.environ.get("CI"), reason="only CI must have node")
    def test_ci_can_run_the_layout(self) -> None:
        """Locally the layout tests skip without node. In CI they must not:
        a skipped guard protects nothing."""
        assert shutil.which("node"), "CI needs node to run the explorer's layout"


class TestAWideFanOutIsGridded:
    """A rank of leaves is packed into a block instead of a column."""

    def _fanout(self, files: int = 60) -> tuple[dict[str, Any], list[str]]:
        out = _canvas(wide_fanout_graph(files=files))
        # f0 and f1 hang off the process, so they are not part of the root's
        # flat list; everything else is a leaf of it.
        return out, [f"data/f{i}.csv" for i in range(2, files)]

    def test_the_leaves_are_not_stacked_in_one_column(self) -> None:
        out, leaves = self._fanout()

        assert len(_columns(out, leaves)) > 1, f"{len(leaves)} leaves still one per row"

    def test_the_layout_opens_inside_the_zoom_the_app_allows(self) -> None:
        """The measurement from the issue, as a bound. ``fitView`` will not pull
        back further than ``FIT_FLOOR``, so a view that cannot be framed at that
        zoom opens cropped — the reader lands on a corner of a graph they asked
        to see whole."""
        out, _ = self._fanout()

        assert _fit(out) >= _app_constant("FIT_FLOOR"), f"opens at {_fit(out):.2f}"

    def test_the_block_is_nearer_square_than_the_column_it_replaces(self) -> None:
        """Not merely "shorter": the point of a grid is that height falls as
        width rises. The bound is the geometry — a column of *n* leaves is at
        least ``n * NODE_H`` tall — not a number read off the current output."""
        out, leaves = self._fanout()
        _, height = _extent(out)

        assert height < len(leaves) * out["nodeH"] / 2

    def test_no_two_nodes_overlap(self) -> None:
        """The invariant packing can break: a grid laid over the nodes that were
        already there draws two entities on top of each other."""
        out, _ = self._fanout()
        boxes = [
            (p["x"], p["y"], p["x"] + out["nodeW"], p["y"] + out["nodeH"])
            for p in out["positions"].values()
        ]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1 :]:
                apart = a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
                assert apart, f"{a} overlaps {b}"

    def test_every_edge_still_points_forward(self) -> None:
        """Left-to-right is what the canvas means by "derived from". A packed
        leaf that lands left of the entity it hangs off reverses that."""
        out, _ = self._fanout()
        pos = out["positions"]

        for edge in out["edges"]:
            assert pos[edge["src"]]["x"] < pos[edge["dst"]]["x"], edge

    def test_an_entity_something_hangs_off_keeps_its_column(self) -> None:
        """Only leaves are packed. A node with an outgoing edge holds structure
        the reader follows, so the chain through it still reads left to right."""
        out, _ = self._fanout()
        pos = out["positions"]

        assert pos["#process"]["x"] > pos["#assay"]["x"]
        assert pos["data/f1.csv"]["x"] > pos["#process"]["x"]


class TestPackingIsForTheRanksThatNeedIt:
    """Everything narrow enough to read keeps the layered layout, which says
    more than a grid does: a column is a rank, and a rank is a step."""

    def test_a_fan_out_at_the_cap_keeps_its_column(self) -> None:
        cap = int(_module_constant("RANK_CAP"))
        out, leaves = TestAWideFanOutIsGridded()._fanout(files=cap + 2)

        assert len(leaves) == cap
        assert len(_columns(out, leaves)) == 1, "a rank at the cap was gridded anyway"

    def test_one_more_than_the_cap_is_gridded(self) -> None:
        cap = int(_module_constant("RANK_CAP"))
        out, leaves = TestAWideFanOutIsGridded()._fanout(files=cap + 3)

        assert len(leaves) == cap + 1
        assert len(_columns(out, leaves)) > 1, "a rank over the cap kept its column"

    def test_a_small_crate_is_untouched(self) -> None:
        """No rank in it comes near the cap, so every column is still a rank."""
        out = _canvas(tabbed_views_graph())

        assert _fit(out) >= _app_constant("FIT_FLOOR")
