"""The interactive entity explorer embedded in the maturity report (#615).

The explorer draws the crate's whole entity graph and lets the reader combine
views. Two things are therefore worth pinning: that the payload it ships says
what the crate says (and says it the same way every run), and that a view's
membership is the *same selection* the corresponding static panel draws — two
renderings of one rule, not two rules that agree today.
"""

from __future__ import annotations

import html
import json
from collections import Counter
import re
import unicodedata
import subprocess
import sys
from html.parser import HTMLParser
from typing import Any

import pytest

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
    render_explorer_page,
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
    build_chemical_inventory,
    build_citation_inventory,
    build_crate_graph,
    build_isa_inventory,
    build_people_inventory,
)
from tests.fixtures.crate_graphs import (
    aop_linked_graph,
    plumbing_heavy_graph,
    process_context_graph,
    tabbed_views_graph,
)

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


class TestCategoryTypeCensus:
    """What the legend labels itself from (#623): the type tags a category's
    nodes actually carry, so the legend and the nodes say the same words."""

    def _cats(self, graph: dict[str, Any] | None = None) -> dict[str, Any]:
        return build_explorer_payload(graph or tabbed_views_graph())["categories"]

    def test_a_category_names_the_tags_its_own_nodes_show(self) -> None:
        payload = build_explorer_payload(tabbed_views_graph())
        cats = payload["categories"]

        for key, category in cats.items():
            shown = {
                node["type"].split(" · ")[0]
                for node in payload["nodes"]
                if node["category"] == key and node["type"]
            }
            assert set(category["types"]) == shown, key

    def test_a_refinement_folds_into_its_base_type(self) -> None:
        """Nodes are tagged ``Dataset · Assay``; the colour is the base type's,
        and spelling every refinement out would make the legend the longest
        thing on the page."""
        cats = self._cats()

        assert cats["container"]["types"] == ["Dataset"]

    def test_the_census_is_ordered_by_how_much_of_the_crate_it_covers(self) -> None:
        """Most common first — the legend spells out only the first two and
        counts the rest away, so the order decides what a reader is told.

        Over the plumbing-heavy crate, whose fallback bucket holds one type
        three times and five others once each: a fixture where every type
        appears equally often could not tell an ordering from its reverse.
        """
        payload = build_explorer_payload(plumbing_heavy_graph())
        types = payload["categories"]["annotation"]["types"]
        counted = Counter(
            node["type"].split(" · ")[0]
            for node in payload["nodes"]
            if node["category"] == "annotation"
        )

        assert len(set(counted.values())) > 1, "the fixture stopped discriminating"
        assert types[0] == max(counted, key=lambda t: (counted[t], t))
        assert [counted[t] for t in types] == sorted(counted.values(), reverse=True)

    def test_two_builds_of_one_crate_order_it_the_same_way(self) -> None:
        """Ties are broken by name, not by dict order: the payload is pinned
        byte-for-byte across runs, so an unstable tie would break the crate's
        reproducibility, not merely the legend's wording."""
        graph = tabbed_views_graph()

        assert self._cats(graph) == self._cats(graph)

    def test_a_category_with_no_types_keeps_its_wording(self) -> None:
        """An off-crate reference has no type because the crate never describes
        it. That key names a provenance status, not a category, and is the one
        label the census cannot supply."""
        cats = self._cats()

        assert cats["ctx"]["types"] == []
        assert cats["ctx"]["label"] == "Referenced outside the crate"


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

    def test_assays_hold_the_key_events_an_assay_measures(self) -> None:
        """What an assay is *for*. The profile links it through
        ``schema:mentions`` (``7_assay_key_event.ttl``), and the view used to
        select the ISA backbone alone (#627)."""
        views = _views(build_explorer_payload(aop_linked_graph()))

        assert "https://aopwiki.org/events/2258" in views["assays"]

    def test_assays_hold_the_pathway_a_study_serves(self) -> None:
        """The AOP hangs off the Study rather than the Assay
        (``6_study_aop.ttl``), so a rule keyed to assays alone would miss it."""
        views = _views(build_explorer_payload(aop_linked_graph()))

        assert "https://aopwiki.org/aops/610" in views["assays"]

    def test_assays_do_not_hold_whatever_else_the_crate_mentions(self) -> None:
        """``mentions`` is a general relation: a crate mentions its own build
        action through it. The view is about the science, so the rule is keyed
        to what is mentioned, not merely to the relation."""
        views = _views(build_explorer_payload(aop_linked_graph()))

        assert "#build" not in views["assays"]

    def test_a_key_event_is_captioned_as_one(self) -> None:
        """A key event is typed ``["KeyEvent", "schema:DefinedTerm"]``, and the
        node caption used to take whichever came first alphabetically — so the
        crate's key events reached the canvas labelled "DefinedTerm",
        indistinguishable from a csvw column's ontology term. Drawing them in
        the Assays view (#627) is worth nothing if the canvas will not name
        them: a domain type outranks the generic one it refines, the way
        MolecularEntity and Sample already do."""
        payload = build_explorer_payload(aop_linked_graph())
        tags = {n["id"]: n["type"] for n in payload["nodes"]}

        assert tags["https://aopwiki.org/events/2258"] == "KeyEvent"
        assert tags["https://aopwiki.org/aops/610"] == "AdverseOutcomePathway"
        assert tags["#term"] == "DefinedTerm", "a plain term is still a plain term"

    def test_a_key_event_is_drawn_as_science_not_plumbing(self) -> None:
        """The caption was only half of it (#627). A key event still reached the
        canvas in the colour the fallback bucket paints csvw columns, licences
        and the build's own action, so a view about the science drew the
        crate's science as plumbing (#643)."""
        cats = {n["id"]: n["category"] for n in build_explorer_payload(aop_linked_graph())["nodes"]}

        assert cats["https://aopwiki.org/events/2258"] == "pathway"
        assert cats["https://aopwiki.org/aops/610"] == "pathway"
        assert cats["#term"] == "annotation", "a plain term still qualifies rather than takes part"

    def test_the_assays_view_and_the_canvas_agree_on_what_a_pathway_is(self) -> None:
        """One list, not two. The view selects what an assay mentions and the
        canvas colours what it selected, and each held its own copy of the two
        ISA-Tox types — so a crate could show a node the view called science and
        the canvas drew as plumbing."""
        graph = aop_linked_graph()
        payload = build_explorer_payload(graph)
        backbone = {n["id"] for n in build_isa_inventory(graph)["nodes"]}
        cats = {n["id"]: n["category"] for n in payload["nodes"]}

        beyond_the_backbone = _views(payload)["assays"] - backbone

        assert beyond_the_backbone, "the fixture's study and assay each mention one"
        assert {cats[i] for i in beyond_the_backbone} == {"pathway"}

    def test_assays_do_not_hold_a_pathway_no_assay_claims(self) -> None:
        """Followed from the backbone, not swept from the crate. The fixture's
        second key event is mentioned only by a note outside the ISA entities,
        so no assay claims it and the view must not show it as though one did."""
        views = _views(build_explorer_payload(aop_linked_graph()))

        assert "https://aopwiki.org/events/9999" not in views["assays"]

    def test_assays_do_not_hold_an_unmentioned_term(self) -> None:
        """Followed, not collected: an ontology term no ISA entity points at
        stays out, whatever its type."""
        views = _views(build_explorer_payload(aop_linked_graph()))

        assert "#term" not in views["assays"]

    def test_assays_still_hold_the_isa_backbone(self) -> None:
        """The pathway is added to the backbone, never in place of it."""
        graph = aop_linked_graph()
        views = _views(build_explorer_payload(graph))

        assert {n["id"] for n in build_isa_inventory(graph)["nodes"]} <= views["assays"]

    def test_labprocesses_holds_the_derivation_chain(self) -> None:
        """The chain is the spine of the view, and every link of it is drawn.

        It used to be the WHOLE of the view, and this test pinned that — down to
        asserting the protocol stayed out as "executed, not derived". #626
        reverses that judgment: a step's protocol is what the reader asking how
        it was done is looking for. The chain is still asserted whole; it is no
        longer asserted alone.
        """
        graph = tabbed_views_graph()
        edges = _derivation_edges(_graph_nodes(graph))

        views = _views(build_explorer_payload(graph))

        assert {e[0] for e in edges} | {e[1] for e in edges} <= views["processes"]
        assert {"#culture", "#exposure", "#line", "#cells", "#table"} <= views["processes"]
        assert "#protocol" in views["processes"]  # executed by #exposure (#626)

    def test_labprocesses_holds_the_protocol_a_step_executes(self) -> None:
        """A step's protocol is how it was done — the thing a reader asking
        "how was this made" most wants — and it is not on the material chain the
        derivation walk follows, so it used to be absent from the one view named
        after the steps (#626)."""
        views = _views(build_explorer_payload(process_context_graph()))

        assert "#protocol" in views["processes"]

    def test_labprocesses_holds_the_assay_a_step_belongs_to(self) -> None:
        """The assay reaches its process through `about`, an edge pointing INTO
        the process, so a walk that only follows a process's own outgoing
        relations cannot find it."""
        views = _views(build_explorer_payload(process_context_graph()))

        assert "#assay" in views["processes"]

    def test_labprocesses_leaves_out_the_context_of_a_step_it_does_not_draw(self) -> None:
        """Followed, not collected.

        The fixture's second step is off the material chain, so the view does
        not draw it — and must not draw its protocol or its assay either. A
        selector that swept up every ``executes`` and ``about`` edge in the
        crate would pass the two tests above and fail this one.
        """
        views = _views(build_explorer_payload(process_context_graph()))

        assert "#orphan-step" not in views["processes"]
        assert "#unused-protocol" not in views["processes"]
        assert "#orphan-assay" not in views["processes"]

    def test_labprocesses_still_holds_the_whole_derivation_chain(self) -> None:
        """The context is added to the chain, never in place of it."""
        views = _views(build_explorer_payload(process_context_graph()))

        assert {"#exposure", "#cells", "result.csv"} <= views["processes"]

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
        """The coverage section and the explorer describe one crate; a reader who
        learned the order in one should not have to relearn it in the other."""
        from builder.writers.maturity_report import _COVERAGE_BLOCKS

        payload = build_explorer_payload(tabbed_views_graph())
        offered = [v["label"] for v in payload["views"]]

        assert offered[0] == "Researcher"
        assert offered[1] == "All entities"  # the whole crate, before its parts
        # The explorer has a view for the derivation chain, which the coverage
        # section has no matrix for; where the two do overlap, the order agrees.
        blocks = [label.replace("&amp;", "&") for _bid, label in _COVERAGE_BLOCKS]
        shared = [label for label in offered if label in blocks]
        assert shared == [label for label in blocks if label in offered]
        assert len(shared) >= 4

    def test_view_labels_are_raw_text(self) -> None:
        """React escapes what it renders; a pre-escaped label would reach the
        reader as `Persons &amp; Organisations`."""
        payload = build_explorer_payload(tabbed_views_graph())

        assert "Persons & Organisations" in {v["label"] for v in payload["views"]}
        assert not any("&amp;" in v["label"] for v in payload["views"])

    def test_the_registry_and_the_payload_offer_the_same_views(self) -> None:
        payload = build_explorer_payload(tabbed_views_graph())

        assert {v["key"] for v in payload["views"]} <= {v.key for v in EXPLORER_VIEWS}


class TestProcessFlavours:
    """#624: the LabProcesses view also opens its four ISA-Tox flavours.

    The flavours are a sub-row that appears only while ``LabProcesses`` is on,
    and they **narrow** what that view contributes rather than adding to the
    canvas: a parent view with active children draws the union of those
    children instead of its own members. That keeps one interaction model —
    views still combine — while answering the review's "just show endpoint
    readout", and it answers what a dropdown cannot: Exposure *and* endpoint
    readout together.

    The selection needs no new classification. A process node's type tag is its
    ISA-Tox discriminator already, so a flavour is the parent's own rule
    restricted to one tag, context and all.
    """

    _KEYS = ("cellculture", "exposure", "endpointreadout", "dataanalysis")

    def test_the_four_flavours_are_the_profiles_own_discriminators(self) -> None:
        """Not a hand-written list: the profile defines exactly these four
        LabProcess kinds, each with a shape file of its own, and a fifth
        invented here would be a category the crate can never carry."""
        from builder.writers.entity_explorer import PROCESS_FLAVOURS
        from builder.writers.provenance_dag import _PROCESS_DISCRIMINATORS

        assert set(PROCESS_FLAVOURS.values()) == set(_PROCESS_DISCRIMINATORS)
        assert tuple(PROCESS_FLAVOURS) == self._KEYS

    def test_a_flavour_is_a_child_of_the_labprocesses_view(self) -> None:
        by_key = {v.key: v for v in EXPLORER_VIEWS}

        assert [by_key[k].parent for k in self._KEYS] == ["processes"] * 4
        assert by_key["processes"].parent is None
        assert by_key["researcher"].parent is None

    def test_the_payload_says_which_view_a_flavour_belongs_to(self) -> None:
        """The browser builds the sub-row from this; without it the flavours
        would render as four more top-level chips, which is the crowding the
        sub-row exists to avoid."""
        payload = build_explorer_payload(tabbed_views_graph())
        parents = {v["key"]: v["parent"] for v in payload["views"]}

        assert parents["exposure"] == "processes"
        assert parents["cellculture"] == "processes"
        assert parents["processes"] is None
        assert parents["all"] is None

    def test_a_flavour_draws_its_own_steps_and_not_the_others(self) -> None:
        views = _views(build_explorer_payload(tabbed_views_graph()))

        assert "#exposure" in views["exposure"]
        assert "#culture" not in views["exposure"]
        assert "#culture" in views["cellculture"]
        assert "#exposure" not in views["cellculture"]

    def test_a_flavour_brings_the_context_its_parent_brings(self) -> None:
        """Narrowing the steps must not strip what makes a step readable — the
        protocol it executes and the assay it serves (#626), and the material it
        consumed and produced. A flavour showing bare process boxes would be a
        worse answer than the unfiltered view."""
        views = _views(build_explorer_payload(process_context_graph()))

        assert {"#exposure", "#protocol", "#assay", "#cells", "result.csv"} <= views["exposure"]

    def test_a_flavour_never_draws_a_step_its_parent_leaves_out(self) -> None:
        """The fixture's DataAnalysis step is off the material chain, so the
        parent view does not draw it — and a flavour is the parent's rule
        restricted, never a fresh sweep of the crate by type. With no step left,
        the flavour has nothing to show and is not offered at all."""
        payload = build_explorer_payload(process_context_graph())
        views = _views(payload)

        assert "#orphan-step" not in views["processes"]
        assert "dataanalysis" not in views
        # No flavour draws it either. (Other views legitimately do — `researcher`
        # shows the experiment whether or not a step sits on the material chain.)
        assert not any(k in views and "#orphan-step" in views[k] for k in self._KEYS)

    def test_the_flavours_between_them_cover_every_step_the_parent_draws(self) -> None:
        """No step falls between the four: a reader who turns all of them on
        sees what LabProcesses shows. Pinned against the parent's own subject so
        a new discriminator in the profile fails here rather than silently
        hiding steps."""
        payload = build_explorer_payload(tabbed_views_graph())
        nodes = {n["id"]: n for n in payload["nodes"]}
        views = _views(payload)

        def steps(key: str) -> set[str]:
            return {i for i in views[key] if nodes[i]["category"] == "process"}

        covered: set[str] = set()
        for key in self._KEYS:
            covered |= steps(key) if key in views else set()
        assert covered == steps("processes")

    def test_a_flavour_chip_counts_its_own_steps(self) -> None:
        """The #625 rule holds for the sub-row too: the chip counts the subject
        it is named for, not the context the selection drags in."""
        payload = build_explorer_payload(tabbed_views_graph())
        counts = {v["key"]: v["count"] for v in payload["views"]}
        members = _views(payload)

        assert counts["exposure"] == 1
        assert counts["cellculture"] == 1
        assert len(members["exposure"]) > counts["exposure"]  # context is drawn, not counted

    def test_the_flavours_follow_their_parent_in_the_offered_order(self) -> None:
        """A sub-row read out by a screen reader follows the chip it refines."""
        offered = [v["key"] for v in build_explorer_payload(tabbed_views_graph())["views"]]
        flavours = [k for k in offered if k in self._KEYS]

        assert offered.index("processes") + 1 == offered.index(flavours[0])
        assert offered[offered.index(flavours[0]) : offered.index(flavours[-1]) + 1] == flavours

    def test_a_flavour_opens_nothing_by_default(self) -> None:
        """An unfiltered LabProcesses is what the view has always meant; a
        flavour pressed on load would silently hide steps from a reader who
        never asked for a filter."""
        payload = build_explorer_payload(tabbed_views_graph())

        assert not any(v["default"] for v in payload["views"] if v["parent"])

    def test_the_app_narrows_a_parent_to_its_active_children(self) -> None:
        """A source-level guard, not a behavioural one: the narrowing runs in
        the browser and this suite has no JS runtime. It pins the two lines that
        carry the rule, so deleting either fails here rather than in a reader's
        browser — the behaviour itself was checked by hand in a headless render.
        """
        source = _app_js()

        assert "CHILDREN" in source
        assert "ex-flavours" in source
        # The sub-row is conditional on its parent being on.
        assert "v.parent" in source

    def test_the_sub_row_is_styled(self) -> None:
        from builder.writers.maturity_report import _load_css

        assert ".ex-flavours" in _load_css()


class TestAChipCountsWhatItIsNamedFor:
    """A view's members include the supporting entities it drags in so the
    selection has context — the files a step touched, the hops that link a
    compound to the work. Counting those on the chip makes every count overstate
    the thing the label names, LabProcesses by threefold (#625).
    """

    def _counts(self, payload: dict[str, Any]) -> dict[str, int]:
        return {v["key"]: v["count"] for v in payload["views"]}

    def _members(self, payload: dict[str, Any]) -> dict[str, int]:
        return {v["key"]: len(v["members"]) for v in payload["views"]}

    def test_a_count_is_the_subject_not_the_selection(self) -> None:
        """The compound view draws the process and table that link a compound to
        the work it was used in. Those are context; the chip says how many
        compounds."""
        payload = build_explorer_payload(tabbed_views_graph())
        chemicals = {
            m["id"] for m in build_chemical_inventory(tabbed_views_graph())["chemicals"]
        }

        assert self._counts(payload)["chemicals"] == len(chemicals)
        assert self._counts(payload)["chemicals"] < self._members(payload)["chemicals"]

    def test_a_subject_the_view_cannot_draw_is_not_counted(self) -> None:
        """The count is the subject *as drawn*.

        A LabProcess with no inputs or outputs is off the derivation chain, so
        the view does not draw it — and a chip that counted it would promise a
        step the reader can never find on the canvas. The crate below holds two
        processes and draws one.
        """
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "Two steps, one drawn",
                    "hasPart": [{"@id": "out.csv"}],
                },
                {
                    "@id": "#drawn",
                    "@type": "LabProcess",
                    "additionalType": "Exposure",
                    "name": "A step on the chain",
                    "object": {"@id": "#cells"},
                    "result": {"@id": "out.csv"},
                },
                {
                    "@id": "#undrawn",
                    "@type": "LabProcess",
                    "additionalType": "DataAnalysis",
                    "name": "A step on no chain",
                },
                {"@id": "#cells", "@type": "Sample", "name": "Cells"},
                {"@id": "out.csv", "@type": "File", "name": "out.csv"},
            ]
        }
        payload = build_explorer_payload(graph)
        members = next(v["members"] for v in payload["views"] if v["key"] == "processes")

        assert "#undrawn" not in members, "the fixture stopped discriminating"
        assert self._counts(payload)["processes"] == 1

    def test_no_count_exceeds_what_the_view_can_draw(self) -> None:
        """The same rule swept over a whole crate."""
        payload = build_explorer_payload(plumbing_heavy_graph())
        counts, members = self._counts(payload), self._members(payload)

        for key in counts:
            assert counts[key] <= members[key], key

    def test_a_view_that_is_its_own_subject_counts_everything(self) -> None:
        """"All entities" and "Researcher" name no narrower thing than what they
        draw, so for them the two numbers are the same — and that is a fact
        about the view, not a special case to exempt."""
        payload = build_explorer_payload(plumbing_heavy_graph())
        counts, members = self._counts(payload), self._members(payload)

        for key in ("all", "researcher"):
            assert counts[key] == members[key], key


class TestTheChipAndTheCoverageBlockAgree:
    """The section further down the report already counts the way the chips
    should. Two numbers for one crate, from two code paths, so they are pinned
    to each other rather than each to a literal."""

    def _cov_n(self, page: str, block_id: str) -> int:
        match = re.search(
            rf'id="{block_id}"[^>]*><summary class="cov-h">.*?<span class="cov-n">(\d+)</span>',
            page,
            re.S,
        )
        assert match, f"{block_id} reported no count"
        return int(match.group(1))

    @pytest.mark.parametrize(
        ("block_id", "key"),
        [
            ("cov-isa", "assays"),
            ("cov-chem", "chemicals"),
            ("cov-cell", "samples"),
            ("cov-people", "people"),
        ],
    )
    def test_the_chip_says_what_the_block_says(self, block_id: str, key: str) -> None:
        graph = tabbed_views_graph()
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        payload = build_explorer_payload(graph)

        count = next(v["count"] for v in payload["views"] if v["key"] == key)
        assert count == self._cov_n(page, block_id)


class TestViewsAgreeWithTheirCoverageBlocks:
    """Each toggle holds what the matching coverage block reports on.

    The two halves of a block used to be a diagram and a matrix over one
    inventory; the diagram is the explorer's now (#618). That makes the pairing
    worth asserting rather than assuming: an entity a block scores for
    identification must be an entity the corresponding view can actually show,
    or the report scores something the reader cannot go and look at.
    """

    def _listed(self, page: str, block_id: str) -> set[str]:
        """The entity names a coverage block's matrix lists."""
        after = page.split(f'id="{block_id}"', 1)[1]
        block = re.split(r'<details class="cov" id=|</section>', after, maxsplit=1)[0]
        names = set()
        for cell in re.findall(r'<span class="cn">(.*?)</span>', block):
            text = html.unescape(re.sub(r"<[^>]+>", "", cell))
            # A row decorates a resolvable name with a link glyph; the crate's
            # own name is the part that is not a symbol.
            names.add("".join(c for c in text if unicodedata.category(c) != "So").strip())
        return names

    def _view_labels(self, payload: dict[str, Any], key: str) -> set[str]:
        members = next(v["members"] for v in payload["views"] if v["key"] == key)
        by_id = {n["id"]: n["label"] for n in payload["nodes"]}
        return {by_id[m] for m in members}

    def _check(self, block_id: str, key: str) -> None:
        graph = tabbed_views_graph()
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        listed = self._listed(page, block_id)

        assert listed, "the block scored nothing, so this proves nothing"
        assert listed <= self._view_labels(build_explorer_payload(graph), key)

    def test_the_molecularentities_block_scores_only_what_that_view_shows(self) -> None:
        self._check("cov-chem", "chemicals")

    def test_the_biological_samples_block_scores_only_what_that_view_shows(self) -> None:
        self._check("cov-cell", "samples")

    def test_the_people_block_scores_only_what_that_view_shows(self) -> None:
        self._check("cov-people", "people")

    def test_the_citations_block_scores_only_what_that_view_shows(self) -> None:
        self._check("cov-cite", "citations")


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
        of a crop of whatever the viewport happened to hold."""
        from builder.writers.maturity_report import _load_css

        css = _load_css().replace("\n", "")

        assert ".mat .ex-app{display:none" in css
        assert ".mat .ex-print-note{display:block" in css

    def test_the_print_rules_are_the_last_word(self) -> None:
        """Print rules are plain declarations inside a media query, so a screen
        rule of equal specificity written later in the file beats them. That is
        not hypothetical: the note that tells a reader where the interactive
        version lives was hidden on paper by the `display:none` that hides it on
        screen, and every assertion about the rule existing still passed.
        """
        from builder.writers.maturity_report import _load_css

        css = _load_css()
        print_block = css.index("@media print{")

        assert ".ex-print-note" in css[print_block:]
        # Every screen rule for the classes print overrides comes before it.
        for selector in (".mat .ex-print-note{display:none", ".mat .ex-app{height:"):
            assert css.rindex(selector) < print_block, selector
        # And nothing follows it: after the block opens, every line is either
        # inside it (indented) or the brace that closes it.
        tail = css[print_block:].splitlines()[1:]
        outside = [ln for ln in tail if ln.strip() and not ln.startswith((" ", "\t", "}"))]
        assert outside == [], outside


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


class TestStandalonePage:
    """The explorer on its own, for the CLI to write and a browser to open."""

    def test_it_is_a_whole_document_carrying_the_section(self) -> None:
        page = render_explorer_page(tabbed_views_graph())

        assert page.startswith("<!DOCTYPE html>")
        assert page.rstrip().endswith("</html>")
        assert 'id="entity-explorer"' in page and f'id="{_DATA_ID}"' in page

    def test_it_is_styled_by_the_report_s_own_stylesheet(self) -> None:
        """The same page in both places: a standalone explorer that drifted from
        the embedded one would be a second thing to keep right."""
        from builder.writers.maturity_report import _load_css

        page = render_explorer_page(tabbed_views_graph())
        head = page.split("<style>", 1)[1].split("</style>", 1)[0]

        assert ".mat .ex-node{" in head
        assert _VENDOR_BANNER in head
        assert _load_css().split("\n", 1)[0] in head

    def test_it_reaches_the_network_for_nothing(self) -> None:
        page = render_explorer_page(tabbed_views_graph())

        markup = re.sub(r"<script.*?</script>", "", page, flags=re.S)
        assert "src=" not in markup and "@import" not in markup

    def test_it_fills_the_window_rather_than_a_report_sized_box(self) -> None:
        """Embedded, the canvas is one section among many and takes a slice of
        the page; alone, the page IS the canvas."""
        page = render_explorer_page(tabbed_views_graph())

        assert "100vh" in page.split("</style>", 1)[0]

    def test_it_is_titled_for_the_crate_it_draws(self) -> None:
        page = render_explorer_page(tabbed_views_graph(), title="S-VHPS22 entity graph")

        assert "<title>S-VHPS22 entity graph</title>" in page

    def test_a_crate_supplied_title_cannot_close_the_tag(self) -> None:
        page = render_explorer_page(tabbed_views_graph(), title='</title><script>x()</script>')

        assert "<script>x()</script>" not in page
        assert "&lt;/title&gt;" in page
