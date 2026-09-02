"""The assay-lane section of the maturity report (#686).

The lane began as a layout the entity explorer could switch to, and never
reached a reader: the explorer draws every id it has selected, the lane places
only the ids it can put on a chain, and combining the lane with any other view —
which the default view made the ordinary case — handed React Flow nodes with no
position and took the whole explorer down with it.

So the lane is a section of its own. It draws one assay at a time, from the
explorer's own data island, and the two sections share one legend because they
colour one crate.

These tests drive ``assay_lane_real_shapes_graph`` wherever a lane's own shape
matters: a three-line assay whose cultures share a rank, and a characterisation
assay with no exposure at all. Neither shape exists in ``assay_lane_graph``, and
that is how the lane last shipped with four defects and a green suite.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from builder.writers.assay_lane import (
    LANE_SCRIPT_COUNT,
    _app_js,
    _view_js,
    render_assay_lane_section,
)
from builder.writers.entity_explorer import build_assay_lanes
from builder.writers.maturity_report import build_maturity_html
from builder.writers.provenance_dag import build_isa_inventory
from tests.fixtures.crate_graphs import (
    assay_lane_graph,
    assay_lane_real_shapes_graph,
    tabbed_views_graph,
)
from tests.fixtures.vhps_golden_crates import vhps_fixture_state

pytestmark = pytest.mark.timeout(180)

_REPO = Path(__file__).resolve().parents[1]


def _section(graph: dict[str, Any] | None = None) -> str:
    return render_assay_lane_section(graph if graph is not None else assay_lane_real_shapes_graph())


def _markup(section: str) -> str:
    """The section without the scripts it inlines — the app is a program that
    happens to contain ``id="…"`` and ``class="…"``, and a structural claim about
    the document has to read the document."""
    return re.sub(r"<script.*?</script>", "", section, flags=re.S)


def _script_tags(section: str) -> list[dict[str, str]]:
    class _Scripts(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.tags: list[dict[str, str]] = []

        def handle_starttag(self, tag: str, attrs: list[Any]) -> None:
            if tag == "script":
                self.tags.append({k: (v or "") for k, v in attrs})

    parser = _Scripts()
    parser.feed(section)
    return parser.tags


class TestAsManyLanesAsTheCrateHasAssays:
    """A deposit has the assays it has. The chips are minted from the crate, and
    a crate with nothing to draw gets no section rather than an empty heading."""

    def test_one_chip_per_assay_the_crate_declares(self) -> None:
        """Pinned to the ISA inventory rather than to a literal, so the two code
        paths that count a crate's assays have to keep agreeing."""
        graph = assay_lane_real_shapes_graph()
        assays = [n for n in build_isa_inventory(graph)["nodes"] if n["level"] == "Assay"]

        assert len(build_assay_lanes(graph)) == len(assays)

    def test_a_crate_with_more_assays_gets_more_lanes(self) -> None:
        """The fixtures differ in how many assays they hold, and the section
        must follow the crate rather than a number written here."""
        two = build_assay_lanes(assay_lane_graph())
        real = build_assay_lanes(assay_lane_real_shapes_graph())

        assert len(two) == 2
        assert len(real) == 2
        assert {lane["key"] for lane in two} != {lane["key"] for lane in real}

    def test_a_crate_with_no_assay_renders_no_section(self) -> None:
        assert render_assay_lane_section(tabbed_views_graph()) == ""

    def test_the_report_leaves_the_section_out_for_such_a_crate(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=tabbed_views_graph())

        assert 'id="assay-lanes"' not in page

    def test_a_report_built_without_the_graph_carries_no_lane(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))

        assert 'id="assay-lanes"' not in page
        assert "AssayLaneView" not in page


class TestTheSectionsMarkup:
    """What the page carries, and what it is allowed to contain."""

    def test_it_names_itself_and_the_frame_the_app_fills(self) -> None:
        """The frame is markup, not something the app builds: a reader with no
        script gets the section's own explanation instead of a blank box, and
        every id the app reaches for is declared in one place."""
        markup = _markup(_section())

        assert 'id="assay-lanes"' in markup
        for element in ("lane-chips", "lane-band", "lane-unfold", "lane-svg",
                        "lane-note", "lane-legend", "lane-panel"):
            assert f'id="{element}"' in markup, element
        assert "<noscript>" in markup

    def test_every_id_the_app_reaches_for_is_in_the_markup(self) -> None:
        """The other half of the same contract, read off the app rather than
        listed twice: a `getElementById` for something the section never emits
        is a section that half-draws and says nothing about it."""
        markup = _markup(_section())
        wanted = set(re.findall(r"getElementById\('([^']+)'\)", _app_js()))
        # The island is the explorer's; the report emits that section first.
        wanted.discard("ex-data")

        missing = sorted(name for name in wanted if f'id="{name}"' not in markup)
        assert missing == [], missing

    def test_it_ships_the_geometry_module_and_the_app(self) -> None:
        tags = _script_tags(_section())

        assert len(tags) == LANE_SCRIPT_COUNT
        assert not any("src" in tag for tag in tags)

    def test_the_geometry_module_loads_before_the_app_that_calls_it(self) -> None:
        section = _section()

        assert section.index("root.AssayLaneView = factory") < section.index(
            "window.AssayLaneView"
        )

    def test_it_loads_nothing_over_the_network(self) -> None:
        """The report is read from inside a crate, on a laptop, offline."""
        ours = _markup(_section()) + _app_js() + _view_js()

        for scheme in ("http://", "https://", "//cdn", "fetch(", "XMLHttpRequest", "WebSocket"):
            assert scheme not in ours, scheme

    def test_no_inlined_script_can_close_the_tag_that_holds_it(self) -> None:
        for body in re.findall(r"<script[^>]*>(.*?)</script>", _section(), re.S):
            assert "</script" not in body

    def test_the_app_never_writes_a_link_or_builds_markup_from_a_string(self) -> None:
        """The crate is untrusted text and the section shows it verbatim, so the
        absence of anchors and of `innerHTML` is load-bearing (#169): every value
        here reaches the page as a text node."""
        source = _app_js()

        for sink in (
            "href",
            "src=",
            "window.open",
            "location.assign",
            "innerHTML",
            "outerHTML",
            "<a ",
        ):
            assert sink not in source, sink

    def test_crate_text_never_becomes_markup(self) -> None:
        graph = assay_lane_real_shapes_graph()
        for entity in graph["@graph"]:
            if entity.get("@id") == "#assay-c":
                entity["name"] = '</script><img onerror="alert(1)">'

        section = render_assay_lane_section(graph)

        assert '<img onerror="alert(1)">' not in section
        assert "</script><img" not in section

    def test_every_class_it_draws_has_a_rule_in_the_report_stylesheet(self) -> None:
        """Same guard the explorer keeps: a class with no rule renders at browser
        defaults, which is how a section quietly loses its layout. Covers the
        classes the app adds at runtime, not just the markup's."""
        from builder.writers.maturity_report import _load_css

        css = _load_css()
        # Ids, not classes: the frame's mount points, the controls the app wires
        # by id, and the arrowhead's marker.
        exempt = {
            "lane-app", "lane-svg", "lane-arrow", "lane-chips", "lane-legend",
            "lane-unfold", "lane-band", "lane-fit", "lane-count", "lane-panel",
        }
        emitted = set(re.findall(r"\blane-[a-z0-9-]+", _markup(_section()) + _app_js()))

        unstyled = sorted(cls for cls in emitted - exempt if f".{cls}" not in css)
        assert unstyled == [], unstyled

    def test_it_prints_a_note_rather_than_a_crop_of_the_drawing(self) -> None:
        from builder.writers.maturity_report import _load_css

        css = _load_css().replace("\n", "")

        assert "ex-print-note" in _markup(_section())
        assert ".mat .ex-app,.mat .lane-app,.mat .ex-side{display:none" in css


class TestItRidesTheExplorersIsland:
    """One crate document on a page that ships inside the crate is already the
    accepted cost of a self-contained report; a second copy for a second section
    would be another. So the lane section carries no island of its own, and the
    report emits the explorer before it."""

    def _page(self) -> str:
        return build_maturity_html(
            vhps_fixture_state("S-VHPS21"), graph=assay_lane_real_shapes_graph()
        )

    def test_the_section_carries_no_island_of_its_own(self) -> None:
        tags = _script_tags(_section())

        assert not any(tag.get("type") == "application/json" for tag in tags)

    def test_the_report_carries_exactly_one_island(self) -> None:
        assert self._page().count('type="application/json"') == 1

    def test_the_explorer_is_rendered_before_the_lanes_that_read_it(self) -> None:
        page = self._page()

        assert page.index('id="entity-explorer"') < page.index('id="assay-lanes"')

    def test_the_lanes_sit_above_the_crate_card_that_closes_the_report(self) -> None:
        page = self._page()

        assert page.index('id="assay-lanes"') < page.index('class="hcard-h">About this RO-Crate')


class TestTheLegendIsTheExplorersOwn:
    """The two sections colour one crate. A reader who learned the colours in the
    explorer must not have to learn them again below it, so the wording is
    computed once in Python and both renderers read the same fields."""

    def test_the_payload_carries_the_wording_rather_than_either_browser(self) -> None:
        from builder.writers.entity_explorer import build_explorer_payload

        categories = build_explorer_payload(assay_lane_real_shapes_graph())["categories"]

        for key, category in categories.items():
            assert category["legend"], key
            assert category["legend_title"], key

    def test_a_key_is_the_class_that_puts_an_entity_in_the_category(self) -> None:
        """The profile's word, not a census of what this crate happens to hold:
        a LabProtocol is also a File, and the census made the protocol key read
        "File, HowTo +1" on the real deposit."""
        from builder.writers.entity_explorer import build_explorer_payload
        from builder.writers.provenance_dag import CATEGORY_STYLES

        categories = build_explorer_payload(assay_lane_real_shapes_graph())["categories"]

        for key, style in CATEGORY_STYLES.items():
            if not style.type:
                continue
            assert categories[key]["legend"] == style.type, key

    def test_the_lane_s_own_keys_are_the_bioschemas_classes(self) -> None:
        from builder.writers.entity_explorer import build_explorer_payload

        categories = build_explorer_payload(assay_lane_real_shapes_graph())["categories"]

        assert categories["process"]["legend"] == "LabProcess"
        assert categories["protocol"]["legend"] == "LabProtocol"
        assert categories["material"]["legend"] == "Sample"
        assert categories["chemical"]["legend"] == "MolecularEntity"
        assert categories["data"]["legend"] == "File"

    def test_the_crate_s_own_tags_ride_on_the_tooltip(self) -> None:
        """Not lost, moved: "what does this crate actually put in that bucket"
        is a question the hover can answer and the strip has no room for."""
        from builder.writers.entity_explorer import build_explorer_payload

        categories = build_explorer_payload(assay_lane_real_shapes_graph())["categories"]
        populated = [c for c in categories.values() if c["types"]]
        assert populated, "the fixture no longer populates any category"

        for category in populated:
            assert category["legend_title"].endswith(", ".join(category["types"]))

    def test_a_category_no_single_class_defines_keeps_its_prose(self) -> None:
        from builder.writers.entity_explorer import build_explorer_payload

        categories = build_explorer_payload(assay_lane_real_shapes_graph())["categories"]

        assert categories["annotation"]["legend"] == categories["annotation"]["label"]
        assert categories["ctx"]["legend"] == categories["ctx"]["label"]

    def test_both_renderers_read_the_same_fields(self) -> None:
        from builder.writers.entity_explorer import _app_js as explorer_app

        for source in (explorer_app(), _app_js()):
            assert "legend_title" in source
            assert ".legend" in source

    def test_a_style_key_reads_the_inspector_s_tag_for_that_node(self) -> None:
        """The hollow swatch already draws the pattern, so the label says only
        what it means — in the words the inspector uses for the same node."""
        from builder.writers.entity_explorer import _app_js as explorer_app
        from builder.writers.entity_explorer import _inspector_js

        inspector = _inspector_js()
        assert "'unreachable from the root'" in inspector
        assert "'described outside the crate'" in inspector

        for source in (explorer_app(), _app_js()):
            assert "['orphan', 'unreachable from the root']" in source
            assert "['outside', 'described outside the crate']" in source
            assert "dashed:" not in source
            assert "dotted:" not in source


class TestOneInspectorForBothViewers:
    """A reader who clicks an entity wants the same answer whichever picture they
    clicked it in, and two copies of a panel is two panels that drift. The same
    goes for the vocabulary underneath it: what `input` is called in the crate,
    and which relations the model draws against their own predicate."""

    def test_the_panel_is_built_in_one_place(self) -> None:
        from builder.writers.entity_explorer import _inspector_js

        module = _inspector_js()
        assert "root.ExplorerInspector = factory" in module
        for marker in ("ex-side-head", "ex-side-tabs", "ex-props", "ex-link-group", "ex-json"):
            assert marker in module, marker

    def test_neither_viewer_keeps_a_panel_of_its_own(self) -> None:
        from builder.writers.entity_explorer import _app_js as explorer_app

        for source in (explorer_app(), _app_js()):
            assert "ExplorerInspector.create" in source
            # The panel's own markup belongs to the module; a viewer that still
            # wrote these is a viewer with a second panel.
            for owned in ("ex-side-head", "ex-props", "ex-link-group"):
                assert owned not in source, owned

    def test_neither_viewer_keeps_a_copy_of_the_vocabulary(self) -> None:
        """`term`, `edgeTerm` and `prop` map the model's words onto the crate's.
        A second copy in a second app is how one page comes to say two things."""
        from builder.writers.entity_explorer import _app_js as explorer_app

        for source in (explorer_app(), _app_js()):
            assert "INSPECTOR.edgeTerm" in source
            assert "function edgeTerm(" not in source
            assert "function term(" not in source
            assert "function prop(" not in source

    def test_the_lane_mounts_it_into_the_same_element_the_explorer_does(self) -> None:
        """Same classes, so the one stylesheet dresses both."""
        markup = _markup(_section())

        assert 'class="ex-side ex-side-empty" id="lane-panel"' in markup


class TestTheGeometryPlacesEverythingItDraws:
    """The invariant whose absence took the explorer down.

    The old caller drew every id it had selected and asked the lane where each
    one went; the lane answered for the ones it could place and said nothing
    about the rest, and React Flow threw on the first node with no position. The
    section now draws exactly what the geometry placed — so this holds by
    construction, and the test is what keeps it holding.
    """

    def _drawings(self) -> dict[str, dict[str, Any]]:
        import json

        from builder.writers.entity_explorer import build_explorer_payload

        exe = shutil.which("node")
        if exe is None:  # pragma: no cover — depends on the machine, not the code
            pytest.skip("node is not installed; the lane module cannot be run")
        payload = build_explorer_payload(assay_lane_real_shapes_graph())
        by_id = {n["id"]: n for n in payload["nodes"]}
        probe = _REPO / "tests" / "fixtures" / "assay_lane_view_probe.js"
        module = _REPO / "builder" / "writers" / "assay_lane_view.js"
        out: dict[str, dict[str, Any]] = {}
        for lane in payload["lanes"]:
            members = set(lane["members"])
            merged: dict[tuple[str, str], dict[str, Any]] = {}
            for edge in payload["edges"]:
                if edge["src"] in members and edge["dst"] in members:
                    key = (edge["src"], edge["dst"])
                    merged.setdefault(key, {"src": key[0], "dst": key[1], "labels": []})
                    merged[key]["labels"].append(edge["label"])
            result = subprocess.run(
                [
                    exe,
                    str(probe),
                    str(module),
                ],
                input=json.dumps(
                    {
                        "nodes": [
                            {"id": i, "category": by_id[i]["category"], "type": by_id[i]["type"]}
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
            out[lane["key"]] = json.loads(result.stdout)
        return out

    def test_every_lane_the_crate_offers_draws(self) -> None:
        drawings = self._drawings()

        assert drawings, "no lane was laid out; this test is inert"
        for key, drawing in drawings.items():
            assert drawing["positions"], key

    def test_the_band_names_what_hangs_below_the_chain(self) -> None:
        """The section lets a reader put the band away, and "everything under
        bandTop" is a rule that stops being true the moment the drawing changes
        shape — so the geometry says which nodes those are, and off what."""
        drawings = self._drawings()
        hung = [h for d in drawings.values() for h in d["band"]]

        assert hung, "the fixture no longer hangs anything under a chain"
        for hang in hung:
            assert hang["label"] in ("executes", "reagent")
            assert hang["id"] in drawings[
                next(k for k, d in drawings.items() if hang in d["band"])
            ]["positions"]

    def test_a_band_member_hangs_off_a_node_that_is_drawn(self) -> None:
        for key, drawing in self._drawings().items():
            for hang in drawing["band"]:
                assert hang["anchor"] in drawing["positions"], (key, hang)


class TestALaneIsSomethingToLinkTo:
    """A lane is what one reader wants to show another, so it rides in the URL
    the way a view already does — under this section's own keys, in the page's
    one hash, so neither section erases the other's link."""

    def test_the_section_reads_and_writes_its_own_keys(self) -> None:
        source = _app_js()

        for key in ("'lane'", "'fold'", "'pick'"):
            assert key in source, key

    def test_neither_section_clobbers_the_other_s_keys(self) -> None:
        """Both start from the hash that is there and delete only what they own.
        A `new URLSearchParams()` on either side would drop the other's state."""
        from builder.writers.entity_explorer import _app_js as explorer_app

        for source in (explorer_app(), _app_js()):
            assert "new URLSearchParams(location.hash.replace(/^#/, ''))" in source
        assert "p.delete('select')" in explorer_app()
        assert "p.delete('pick')" in _app_js()

    def test_the_boxes_carry_the_type_the_explorer_s_nodes_carry(self) -> None:
        """Same caption in both pictures, so a box means one thing."""
        assert "lane-tag" in _app_js()
        assert "node.type.toUpperCase()" in _app_js()
