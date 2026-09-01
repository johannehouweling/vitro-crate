"""The browser half of the explorer, checked by something that can run it.

The Python suite renders the page and reads its markup, so it sees the scripts
as text. It cannot see that a function the app calls is no longer defined —
the app's functions live inside an IIFE and are never exported, so nothing
observes the hole until the page throws in front of a reader. That is exactly
how three helpers (``Glyph``, ``shortId``, ``layerName``) were deleted by a
careless edit and shipped: every Python test stayed green.

These run node over the shipped files: it parses them the way the browser will,
and reports any name they reference but never define.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROBE = _REPO / "tests" / "fixtures" / "js_names_probe.js"

# What the page supplies to the app from somewhere other than the app: the
# vendored bundles and the two DOM globals it reads. A name resolved here is
# resolved because the page really provides it — the list is the contract, so
# adding to it is a deliberate act and not a way to quiet the check.
_PAGE_GLOBALS = (
    "React", "ReactDOM", "htm", "dagre", "window", "document", "console",
    "navigator", "history", "location",
)

_SCRIPTS = (
    "entity_explorer.js",
    "entity_explorer_layout.js",
    "explorer_inspector.js",
    "assay_lane_view.js",
    "assay_lane_app.js",
    "payload_codec.js",
)


def _node() -> str:
    exe = shutil.which("node")
    if exe is None:  # pragma: no cover — depends on the machine, not the code
        pytest.skip("node is not installed; the explorer's scripts cannot be parsed")
    return exe


def _free_names(script: Path) -> list[str]:
    result = subprocess.run(
        [_node(), str(_PROBE), str(script), *_PAGE_GLOBALS],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["free"]


@pytest.mark.skipif(not os.environ.get("CI"), reason="only CI must have node")
def test_ci_can_parse_the_scripts() -> None:
    """Locally these skip without node. In CI they must not: a skipped guard
    protects nothing, and this one guards against a page that cannot run."""
    assert shutil.which("node"), "CI needs node to parse the explorer's scripts"


@pytest.mark.parametrize("name", _SCRIPTS)
def test_the_script_parses(name: str) -> None:
    """Node's own parser, so a syntax error is caught here rather than by the
    first reader to open a crate."""
    _free_names(_REPO / "builder" / "writers" / name)  # the probe parses before it reports


@pytest.mark.parametrize("name", _SCRIPTS)
def test_it_defines_every_name_it_calls(name: str) -> None:
    free = _free_names(_REPO / "builder" / "writers" / name)

    assert free == [], f"{name} calls names nothing defines: {', '.join(free)}"


def test_the_check_can_fail(tmp_path: Path) -> None:
    """The control. A guard against deleted code is worth only as much as its
    ability to notice one, so delete one and watch it notice.

    Cuts a whole function, so the file still parses — this proves the *name*
    check works, not merely that node rejects broken syntax.
    """
    source = (_REPO / "builder" / "writers" / "entity_explorer.js").read_text(encoding="utf-8")
    start = source.index("  function summary(graph, hits) {")
    end = source.index("\n  }\n", start) + len("\n  }\n")
    wounded = tmp_path / "entity_explorer.js"
    wounded.write_text(source[:start] + source[end:], encoding="utf-8")

    assert _free_names(wounded) == ["summary"]


class TestThePageLoadsInABrowser:
    """The UMD branch the report actually runs.

    Every other check in this repo reaches the layout modules through
    ``require()``, which takes the CommonJS branch of their wrapper. The page
    takes the other one: plain ``<script>`` tags, attaching to ``window``. A
    module that failed to attach itself, or one placed before the module it
    reads at factory time, would pass the whole suite and throw in front of a
    reader (#686).
    """

    def _run(self, scripts: list[str], expect: list[str]) -> dict:
        probe = _REPO / "tests" / "fixtures" / "browser_load_probe.js"
        result = subprocess.run(
            [_node(), str(probe)],
            input=json.dumps({"scripts": scripts, "expect": expect}),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def _bodies(self, section: str) -> list[str]:
        import re

        return re.findall(r"<script[^>]*>(.*?)</script>", section, re.S)

    def _explorer_scripts(self) -> list[str]:
        """dagre and the canvas's layout module, in the order the page emits."""
        from builder.writers.entity_explorer import render_explorer_section
        from tests.fixtures.crate_graphs import assay_lane_graph

        wanted = ("dagre", "root.ExplorerLayout = factory")
        return [
            body
            for body in self._bodies(render_explorer_section(assay_lane_graph()))
            if any(marker in (body if marker != "dagre" else body[:2000]) for marker in wanted)
        ]

    def _lane_scripts(self) -> list[str]:
        """The lane's geometry module alone. Its app is left out on purpose: the
        probe gives a script `window` but no `document`, which is the right
        contract for a module that must be pure geometry and would be the wrong
        one for the drawing."""
        return [
            body for body in self._bodies(_lane_section())
            if "root.AssayLaneView = factory" in body
        ]

    def test_the_canvas_layout_module_attaches_itself_to_the_window(self):
        out = self._run(self._explorer_scripts(), ["ExplorerLayout"])
        assert out["defined"] == {"ExplorerLayout": True}
        assert out["sizes"]["ExplorerLayout"] == {"NODE_W": 200, "NODE_H": 44}

    def test_the_lane_geometry_module_attaches_itself_to_the_window(self):
        """The lane section is loaded on its own, so its geometry module must
        attach without the explorer's — it no longer reads that one's sizes."""
        out = self._run(self._lane_scripts(), ["AssayLaneView"])
        assert out["defined"] == {"AssayLaneView": True}

    def test_the_lane_sizes_its_own_boxes(self):
        """It used to take them from the canvas beside it. The lane is its own
        section now, drawn flat at a scale chosen for a nine-column chain, and a
        dagre canvas's 200x44 box would make that chain too wide to read."""
        out = self._run(self._lane_scripts(), ["AssayLaneView"])
        assert out["sizes"]["AssayLaneView"] == {"NODE_W": 152, "NODE_H": 34}

    def test_the_lane_module_loads_before_the_app_that_calls_it(self):
        section = _lane_section()
        assert section.index("root.AssayLaneView = factory") < section.index(
            "window.AssayLaneView"
        )


def _lane_section() -> str:
    from builder.writers.assay_lane import render_assay_lane_section
    from tests.fixtures.crate_graphs import assay_lane_graph

    return render_assay_lane_section(assay_lane_graph())
