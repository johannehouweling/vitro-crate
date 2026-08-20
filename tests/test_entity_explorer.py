"""The interactive entity explorer embedded in the maturity report (#615).

The explorer draws the crate's whole entity graph and lets the reader combine
views. Two things are therefore worth pinning: that the payload it ships says
what the crate says (and says it the same way every run), and that a view's
membership is the *same selection* the corresponding static panel draws — two
renderings of one rule, not two rules that agree today.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from typing import Any

from builder.writers.entity_explorer import (
    EXPLORER_SCRIPT_COUNT,
    EXPLORER_VIEWS,
    VENDOR_MANIFEST,
    _APP_ID,
    _DATA_ID,
    _VENDOR_DIR,
    _app_js,
    build_explorer_payload,
    explorer_css,
    render_explorer_section,
)
from builder.writers.maturity_report import build_maturity_html
from builder.writers.provenance_dag import (
    CATEGORY_STYLES,
    _CTX_GLYPH,
    _derivation_edges,
    _graph_nodes,
    build_cellline_inventory,
    build_chemical_inventory,
    build_citation_inventory,
    build_crate_graph,
    build_isa_inventory,
    build_people_inventory,
    render_celllines_svg,
    render_chemicals_svg,
    render_citations_svg,
    render_people_svg,
)
from tests.fixtures.crate_graphs import plumbing_heavy_graph, tabbed_views_graph

_VENDOR_BANNER = "@xyflow/react (styles) 12.11.3"
"""A string only React Flow's vendored stylesheet puts in the page."""
from tests.fixtures.vhps_golden_crates import vhps_fixture_state


def _views(payload: dict[str, Any]) -> dict[str, set[str]]:
    """View key → member ids."""
    return {view["key"]: set(view["members"]) for view in payload["views"]}


def _types(entity: dict[str, Any]) -> set[str]:
    """The entity's ``@type`` plus ``additionalType``, prefixes stripped."""
    out: set[str] = set()
    for key in ("@type", "additionalType"):
        value = entity.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                out.add(item.split(":")[-1])
    return out


def _ids(graph: dict[str, Any], predicate) -> set[str]:
    """Ids of the raw entities matching *predicate*, as the spec describes them."""
    return {e["@id"] for e in graph["@graph"] if "@id" in e and predicate(e)}


class TestPayloadShape:
    """What the data island carries."""

    def test_it_round_trips_through_json(self) -> None:
        """It is serialised into the page; a set or tuple in it is a crash at
        render time, not a wrong pixel."""
        payload = build_explorer_payload(tabbed_views_graph())

        assert json.loads(json.dumps(payload)) == payload

    def test_it_is_the_same_bytes_under_any_hash_seed(self) -> None:
        """`build_crate_graph` yields its off-crate stubs out of a set, so the
        payload has to impose an order of its own — otherwise two builds of one
        crate ship different reports and every diff of them is noise."""
        script = (
            "import json;"
            "from builder.writers.entity_explorer import build_explorer_payload;"
            "from tests.fixtures.crate_graphs import plumbing_heavy_graph;"
            "print(json.dumps(build_explorer_payload(plumbing_heavy_graph())))"
        )
        runs = {
            subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=True,
                env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            ).stdout
            for seed in ("0", "1", "12345")
        }

        assert len(runs) == 1

    def test_labels_are_raw_text_not_markup(self) -> None:
        """The model escapes labels for its SVG; the explorer renders through the
        DOM, which escapes again. Shipping the escaped form would show a reader
        `A &amp; B` where the crate says `A & B`."""
        graph = tabbed_views_graph()
        graph["@graph"][1]["name"] = "Tissue <slice> & buffer"

        payload = build_explorer_payload(graph)

        root = next(n for n in payload["nodes"] if n["id"] == "./")
        assert root["label"] == "Tissue <slice> & buffer"

    def test_every_category_carries_its_colour_and_glyph(self) -> None:
        payload = build_explorer_payload(tabbed_views_graph())

        assert set(payload["categories"]) == set(CATEGORY_STYLES) | {"ctx"}
        for key, style in CATEGORY_STYLES.items():
            assert payload["categories"][key]["colour"] == style.colour
            assert payload["categories"][key]["glyph"] == style.glyph
            assert payload["categories"][key]["label"] == style.label
        assert payload["categories"]["ctx"]["glyph"] == _CTX_GLYPH

    def test_it_carries_the_crate_document_verbatim(self) -> None:
        """Both JSON modes read from it, so it must be the document the crate
        ships — including the entities the graph model drops as plumbing."""
        graph = plumbing_heavy_graph()

        payload = build_explorer_payload(graph)

        assert payload["document"] == graph
        assert any(e["@id"] == "ro-crate-metadata.json" for e in payload["document"]["@graph"])

    def test_a_bare_graph_list_is_wrapped_as_a_document(self) -> None:
        payload = build_explorer_payload(tabbed_views_graph()["@graph"])

        assert payload["document"]["@graph"][1]["@id"] == "./"

    def test_nodes_and_edges_are_the_graph_model(self) -> None:
        graph = plumbing_heavy_graph()
        model = build_crate_graph(graph, layer="all", all_edges=True)

        payload = build_explorer_payload(graph)

        assert {n["id"] for n in payload["nodes"]} == {n["id"] for n in model["nodes"]}
        assert len(payload["edges"]) == len(model["edges"])
        assert payload["root"] == model["root"]
        assert payload["counts"]["nodes"] == len(model["nodes"])

    def test_a_node_states_how_the_crate_reaches_it(self) -> None:
        """The flags the side panel shows: an entity nothing points at, and an
        `@id` with no entity behind it, must both be readable as such."""
        payload = build_explorer_payload(plumbing_heavy_graph())
        by_id = {n["id"]: n for n in payload["nodes"]}

        assert by_id["#missing-instrument"]["status"] == "dangling"
        assert by_id["./"]["status"] == "in_crate"
        assert by_id["./"]["orphan"] is False
        assert set(by_id["#sample"]) >= {"layer", "reach", "identifier_backed", "category"}


class TestViewMembership:
    """A view is a selection over the model, and it is the panel's selection."""

    def test_all_entities_is_what_the_overview_draws(self) -> None:
        """The overview tile map draws every node with a layer — i.e. everything
        the crate itself describes."""
        graph = plumbing_heavy_graph()
        model = build_crate_graph(graph, layer="all", all_edges=True)

        views = _views(build_explorer_payload(graph))

        assert views["all"] == {n["id"] for n in model["nodes"] if n["layer"] is not None}

    def test_every_entity_the_crate_describes_is_in_some_view(self) -> None:
        """A node in no view can never be drawn, and the report would be quietly
        hiding part of the crate it is reporting on."""
        graph = plumbing_heavy_graph()
        payload = build_explorer_payload(graph)
        shown = set().union(*(set(v["members"]) for v in payload["views"]))

        assert {n["id"] for n in payload["nodes"] if n["status"] == "in_crate"} <= shown

    def test_researcher_hides_the_machinery(self) -> None:
        """Parameters, column definitions, ontology terms, the licence, the
        profile, the build action and its software are how the crate is made,
        not what was done at the bench."""
        graph = plumbing_heavy_graph()

        views = _views(build_explorer_payload(graph))

        for hidden in (
            "#param_dose",
            "#col_dose",
            "http://purl.obolibrary.org/obo/NCIT_C25488",
            "#schema",
            "https://creativecommons.org/licenses/by/4.0/",
            "https://w3id.org/ro/crate/isa-tox/1.0",
            "#run",
            "#tool",
            "#missing-instrument",
        ):
            assert hidden not in views["researcher"], hidden

    def test_researcher_keeps_the_experiment(self) -> None:
        graph = plumbing_heavy_graph()

        views = _views(build_explorer_payload(graph))

        for kept in ("./", "#step", "#sample", "assay.csv", "readme.txt"):
            assert kept in views["researcher"], kept

    def test_researcher_keeps_people_and_papers_though_they_sit_in_the_base_layer(
        self,
    ) -> None:
        """Persons, Organisations and articles are layer 1, the same layer as the
        packaging, so a layer-based rule would drop exactly the credit a reader
        looks for."""
        views = _views(build_explorer_payload(tabbed_views_graph()))

        assert "https://orcid.org/0000-0002-1825-0097" in views["researcher"]
        assert "https://ror.org/05gq02987" in views["researcher"]
        assert "https://doi.org/10.1007/s00204-024-03787-2" in views["researcher"]

    def test_researcher_keeps_a_root_the_crate_never_typed(self) -> None:
        """An untyped root falls to the catch-all category, and dropping it would
        leave the whole crate with nothing to hang from."""
        graph = {"@graph": [{"@id": "./", "name": "Untyped"}, {"@id": "#s", "@type": "Sample"}]}

        views = _views(build_explorer_payload(graph))

        assert "./" in views["researcher"]

    def test_files_holds_every_file_and_every_dataset(self) -> None:
        graph = plumbing_heavy_graph()

        views = _views(build_explorer_payload(graph))

        expected = _ids(graph, lambda e: {"File", "Dataset"} & _types(e))
        assert views["files"] == expected - {"ro-crate-metadata.json"}

    def test_assays_holds_the_isa_backbone(self) -> None:
        graph = tabbed_views_graph()

        views = _views(build_explorer_payload(graph))

        assert views["assays"] == {n["id"] for n in build_isa_inventory(graph)["nodes"]}
        assert views["assays"] == {"./"}  # the fixture declares one Investigation

    def test_labprocesses_holds_the_derivation_chain(self) -> None:
        graph = tabbed_views_graph()
        edges = _derivation_edges(_graph_nodes(graph))

        views = _views(build_explorer_payload(graph))

        assert views["processes"] == {e[0] for e in edges} | {e[1] for e in edges}
        assert {"#culture", "#exposure", "#line", "#cells", "#table"} <= views["processes"]
        assert "#protocol" not in views["processes"]  # executed, not derived

    def test_molecularentities_brings_the_route_that_links_a_compound(self) -> None:
        """A compound is linked to its process through the condition table. The
        panel names the route; the toggle has to include it or the compound
        floats with no edge to the work that used it."""
        graph = tabbed_views_graph()

        views = _views(build_explorer_payload(graph))

        assert "#compound" in views["chemicals"]
        assert {"#table", "#exposure"} <= views["chemicals"]

    def test_biological_samples_bring_their_process(self) -> None:
        graph = tabbed_views_graph()

        views = _views(build_explorer_payload(graph))

        assert "#line" in views["samples"]
        assert "#culture" in views["samples"]

    def test_persons_and_organisations_hold_everyone_and_who_credits_them(self) -> None:
        graph = tabbed_views_graph()

        views = _views(build_explorer_payload(graph))

        assert _ids(graph, lambda e: {"Person", "Organization"} & _types(e)) <= views["people"]
        assert "./" in views["people"]  # the entity carrying the `author` edge

    def test_citations_hold_each_article_and_its_authors(self) -> None:
        graph = tabbed_views_graph()

        views = _views(build_explorer_payload(graph))

        assert _ids(graph, lambda e: "ScholarlyArticle" in _types(e)) <= views["citations"]
        assert "https://orcid.org/0000-0002-1825-0097" in views["citations"]

    def test_a_view_with_nothing_to_show_is_not_offered(self) -> None:
        """The tabs already drop an empty view rather than show an empty panel; a
        toggle that changes nothing when pressed is worse, because the reader
        cannot tell it apart from one that is broken."""
        graph = {"@graph": [{"@id": "./", "@type": "Dataset", "name": "Bare"}]}

        views = _views(build_explorer_payload(graph))

        assert "chemicals" not in views
        assert "citations" not in views
        assert views["all"] == {"./"}

    def test_members_are_sorted_and_known_to_the_model(self) -> None:
        """An id the model never yielded cannot be drawn; shipping one would put
        a count on a chip that the canvas then fails to honour."""
        payload = build_explorer_payload(plumbing_heavy_graph())
        known = {n["id"] for n in payload["nodes"]}

        for view in payload["views"]:
            assert view["members"] == sorted(view["members"]), view["key"]
            assert set(view["members"]) <= known, view["key"]

    def test_researcher_is_the_view_that_opens(self) -> None:
        payload = build_explorer_payload(tabbed_views_graph())

        defaults = [v["key"] for v in payload["views"] if v["default"]]
        assert defaults == ["researcher"]

    def test_the_views_are_offered_in_the_report_s_own_order(self) -> None:
        """The tabbed section and the explorer describe one crate; a reader who
        learned the tab order should not have to relearn it two sections down."""
        from builder.writers.maturity_report import _VIEWS

        payload = build_explorer_payload(tabbed_views_graph())
        offered = [v["label"] for v in payload["views"]]

        assert offered[0] == "Researcher"
        tabs = [label.replace("&amp;", "&") for _rid, _pid, label in _VIEWS]
        assert offered[1:] == [label for label in tabs if label in offered]

    def test_view_labels_are_raw_text(self) -> None:
        """React escapes what it renders; a pre-escaped label would reach the
        reader as `Persons &amp; Organisations`."""
        payload = build_explorer_payload(tabbed_views_graph())

        assert "Persons & Organisations" in {v["label"] for v in payload["views"]}
        assert not any("&amp;" in v["label"] for v in payload["views"])

    def test_the_registry_and_the_payload_offer_the_same_views(self) -> None:
        payload = build_explorer_payload(tabbed_views_graph())

        assert {v["key"] for v in payload["views"]} <= {v.key for v in EXPLORER_VIEWS}


class TestViewsAgreeWithTheirPanels:
    """Each toggle shows what the panel it descends from drew.

    The static panels are the reviewed answer to "what belongs in this view".
    Comparing against what they *draw* — not against the inventory both happen
    to call — is what keeps a future edit to one from silently diverging.
    """

    def _drawn(self, svg: str, payload: dict[str, Any]) -> set[str]:
        """Labels of the entities the panel draws.

        A node's tooltip reads ``Label — Tag``; the diagram also carries a
        summary title that names no entity, so only titles whose head is some
        node's label count as something drawn.
        """
        import html
        import re

        labels = {n["label"] for n in payload["nodes"]}
        heads = (
            html.unescape(title).split(" — ")[0].strip()
            for title in re.findall(r"<title>([^<]*)</title>", svg)
        )
        return {head for head in heads if head in labels}

    def _labels(self, payload: dict[str, Any], key: str) -> set[str]:
        members = next(v["members"] for v in payload["views"] if v["key"] == key)
        by_id = {n["id"]: n["label"] for n in payload["nodes"]}
        return {by_id[m] for m in members}

    def _assert_panel_within_view(
        self, svg: str, payload: dict[str, Any], key: str
    ) -> None:
        drawn = self._drawn(svg, payload)

        assert drawn, "the panel drew no entity, so this proves nothing"
        assert drawn <= self._labels(payload, key)

    def test_the_chemicals_panel_draws_only_members_of_the_chemicals_view(self) -> None:
        graph = tabbed_views_graph()
        payload = build_explorer_payload(graph)
        svg = render_chemicals_svg(build_chemical_inventory(graph))

        self._assert_panel_within_view(svg, payload, "chemicals")

    def test_the_celllines_panel_draws_only_members_of_the_samples_view(self) -> None:
        graph = tabbed_views_graph()
        payload = build_explorer_payload(graph)
        svg = render_celllines_svg(build_cellline_inventory(graph))

        self._assert_panel_within_view(svg, payload, "samples")

    def test_the_people_panel_draws_only_members_of_the_people_view(self) -> None:
        graph = tabbed_views_graph()
        payload = build_explorer_payload(graph)
        svg = render_people_svg(build_people_inventory(graph))

        self._assert_panel_within_view(svg, payload, "people")

    def test_the_citations_panel_draws_only_members_of_the_citations_view(self) -> None:
        graph = tabbed_views_graph()
        payload = build_explorer_payload(graph)
        svg = render_citations_svg(build_citation_inventory(graph))

        self._assert_panel_within_view(svg, payload, "citations")


class TestVendoredAssets:
    """The page carries everything it runs, and says what it carries."""

    def test_the_manifest_pins_every_file_it_ships(self) -> None:
        """A vendored bundle is code nobody in this repo reviews line by line.
        Pinning the digest is what makes an edit to one — accidental or not —
        fail a test instead of shipping inside every crate built afterwards."""
        import hashlib

        for entry in VENDOR_MANIFEST:
            blob = (_VENDOR_DIR / entry["file"]).read_bytes()

            assert hashlib.sha256(blob).hexdigest() == entry["sha256"], entry["file"]
            assert entry["license"] and entry["source_url"].startswith("https://")
            assert entry["version"] in entry["source_url"], entry

    def test_it_ships_the_bundles_the_page_needs(self) -> None:
        names = {entry["name"] for entry in VENDOR_MANIFEST}

        assert {"react", "react-dom", "@xyflow/react", "@dagrejs/dagre", "htm"} <= names

    def test_the_page_credits_each_bundle_by_name_version_and_licence(self) -> None:
        page = render_explorer_section(tabbed_views_graph())

        for entry in VENDOR_MANIFEST:
            if entry["file"].endswith(".css"):
                continue
            assert f"{entry['name']} {entry['version']}" in page, entry["name"]
            assert entry["license"] in page

    def test_the_vendored_stylesheet_declares_no_entity_colour(self) -> None:
        """The category palette has exactly one source (`category_css`). A
        vendored rule that set one would be a second, unreviewed, palette."""
        css = explorer_css()

        assert "--cat-" not in css
        assert ".react-flow" in css


class TestExplorerSection:
    """The markup, and what it is allowed to contain."""

    def _section(self) -> str:
        return render_explorer_section(tabbed_views_graph())

    def _script_tags(self, page: str) -> list[dict[str, str]]:
        """Attributes of every ``<script>`` start tag, by parsing — not by
        counting a substring: React DOM's own bundle contains the text
        ``<script>`` inside a string literal, and would inflate any such count.
        """

        class _Scripts(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.tags: list[dict[str, str]] = []

            def handle_starttag(self, tag: str, attrs: list[Any]) -> None:
                if tag == "script":
                    self.tags.append({k: (v or "") for k, v in attrs})

        parser = _Scripts()
        parser.feed(page)
        return parser.tags

    def test_it_loads_nothing_over_the_network(self) -> None:
        """The report is read from inside a crate, on a laptop, offline.

        Asserted over the markup plus this repo's own script — never over the
        script bodies the page inlines. A URL in there is a string, not a
        request: React DOM's source carries XML namespaces by the dozen, and the
        payload carries every ORCID and DOI the crate names.
        """
        section = self._section()
        tags = self._script_tags(section)

        assert len(tags) == EXPLORER_SCRIPT_COUNT
        assert not any("src" in tag for tag in tags)
        assert "@import" not in section
        ours = re.sub(r"<script.*?</script>", "", section, flags=re.S) + _app_js()
        for scheme in ("http://", "https://", "//cdn", "fetch(", "XMLHttpRequest", "WebSocket"):
            assert scheme not in ours, scheme

    def test_exactly_one_script_is_the_data_island(self) -> None:
        tags = self._script_tags(self._section())

        islands = [t for t in tags if t.get("type") == "application/json"]
        assert [t["id"] for t in islands] == [_DATA_ID]

    def test_no_inlined_script_can_close_the_tag_that_holds_it(self) -> None:
        """Everything here is inlined verbatim; a `</script` anywhere in a body
        ends the block early and spills code into the document as text."""
        for body in re.findall(r"<script[^>]*>(.*?)</script>", self._section(), re.S):
            assert "</script" not in body.lower()
            assert "<!--" not in body

    def test_crate_text_never_becomes_markup(self) -> None:
        """The crate is untrusted text (#169). The island is JSON, so the escape
        that matters is the one that stops it being read as HTML."""
        graph = tabbed_views_graph()
        graph["@graph"][1]["name"] = '</script><img src=x onerror=alert(1)>'

        section = render_explorer_section(graph)

        island = section.split(f'id="{_DATA_ID}" type="application/json">', 1)[1]
        island = island.split("</script>", 1)[0]
        assert "<" not in island and ">" not in island and "&" not in island
        # …and the crate's own text survives the escaping intact.
        payload = json.loads(island)
        root = next(n for n in payload["nodes"] if n["id"] == "./")
        assert root["label"] == '</script><img src=x onerror=alert(1)>'
        assert "<img" not in section

    def test_the_section_names_itself_and_its_mount_point(self) -> None:
        section = self._section()

        assert 'id="entity-explorer"' in section
        assert f'id="{_APP_ID}"' in section
        assert "<noscript>" in section

    def test_every_class_it_draws_has_a_rule_in_the_report_stylesheet(self) -> None:
        """Same guard the report already applies to itself: a class with no rule
        renders at browser defaults, which is how a section quietly loses its
        layout. Covers the classes the browser adds too, not just Python's."""
        from builder.writers.maturity_report import _load_css

        css = _load_css()
        # `ex-app`/`ex-data` name elements, and `ex-c` is the custom property a
        # node takes its category colour from; none of the three is a class.
        exempt = {"ex-app", "ex-data", "ex-c"}
        emitted = set(re.findall(r"\bex-[a-z0-9-]+", self._section() + _app_js()))

        unstyled = sorted(cls for cls in emitted - exempt if f".{cls}" not in css)
        assert unstyled == [], unstyled


class TestEmbeddedInTheReport:
    """How the section reaches the page the crate ships."""

    def test_the_report_carries_the_explorer_when_it_has_the_crate_graph(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=tabbed_views_graph())

        assert 'id="entity-explorer"' in page
        assert _VENDOR_BANNER in page
        assert f'id="{_DATA_ID}"' in page

    def test_a_state_only_report_carries_no_script_at_all(self) -> None:
        """Without the crate's graph there is nothing to explore, and the report
        stays the plain document it has always been."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))

        assert "<script" not in page.lower()
        assert _VENDOR_BANNER not in page
        assert "entity-explorer" not in page

    def test_the_explorer_styles_ride_in_the_document_stylesheet(self) -> None:
        """One `<style>` in the head, as the report has always had: a second one
        in the body would land inside every section-scoped assertion in the
        suite, and outside the print rules that expand the page."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=tabbed_views_graph())
        head_style = page.split("<style>", 1)[1].split("</style>", 1)[0]

        assert _VENDOR_BANNER in head_style
        assert ".react-flow__node" in head_style
        assert page.count("<style>") == 1

    def test_it_still_prints(self) -> None:
        """A pannable canvas is a screen affordance; paper gets the note instead
        of a cropped screenshot of whatever the viewport happened to hold."""
        from builder.writers.maturity_report import _load_css

        css = _load_css().replace("\n", "")

        assert ".mat .ex-app{display:none" in css
        assert ".mat .ex-print-note{display:block" in css


class TestTheExplorerBuildsNoLinks:
    """A crate's own strings never become somewhere the browser can go.

    The payload ships the crate verbatim, `javascript:` URLs and all — that is
    what the crate says, and the JSON panel exists to show it. Which makes the
    absence of anchors load-bearing rather than incidental: the explorer renders
    every reference as a button that moves the selection, and every value as
    text. The day someone renders one as `<a href>` instead, a crate becomes
    able to put script behind a link in every report built from it.
    """

    def test_a_crate_url_can_never_become_a_link(self) -> None:
        graph = tabbed_views_graph()
        graph["@graph"][1]["url"] = "javascript:alert(1)"

        section = render_explorer_section(graph)

        markup = re.sub(r"<script.*?</script>", "", section, flags=re.S)
        assert "javascript:" not in markup
        assert "href" not in markup

    def test_the_app_never_writes_an_href_or_a_src(self) -> None:
        source = _app_js()

        # `src` on its own would match every edge's source field; these are the
        # forms that actually put a URL somewhere the browser follows or parses.
        for sink in ("href", "src=", "window.open", "location.assign", "innerHTML",
                     "outerHTML", "dangerouslySetInnerHTML", "<a "):
            assert sink not in source, sink
