"""One assay drawn as a lane, and the compounds that were a hop out of reach (#686).

The LabProcesses view draws 74 nodes for 15 steps on a real deposit, and its
Exposure sub-view draws none of the compounds — a researcher cannot visually
inspect the flow, which is what the view is for.

Two changes, tested here against the same crate:

* compounds join the process views, which they were missing by one hop rather
  than by a modelling gap;
* an assay becomes a selectable unit, drawing its own chain and nothing else.

Layout is a separate module (``assay_lane_view.js``) and is tested against the
shipped JavaScript in ``test_assay_lane_view.py``; the section that draws it is
tested in ``test_assay_lane_section.py``.
"""

from __future__ import annotations

import pytest

from builder.writers.entity_explorer import (
    _compact,
    _Crate,
    _select_assay_lane,
    _select_processes,
    build_assay_lanes,
    build_explorer_payload,
)
from tests.fixtures.crate_graphs import assay_lane_graph, tabbed_views_graph

pytestmark = pytest.mark.timeout(180)


@pytest.fixture(scope="module")
def crate() -> _Crate:
    return _Crate(assay_lane_graph())


@pytest.fixture(scope="module")
def payload() -> dict:
    return build_explorer_payload(assay_lane_graph())


@pytest.fixture(scope="module")
def lanes() -> list[dict]:
    return build_assay_lanes(assay_lane_graph())


class TestCompoundsReachTheProcessViews:
    """#650/#686: the exposure's compounds are the substances under test.

    They hang off the condition table the exposure executes, because ISA
    restricts ``schema:object`` to File/Sample/BioSample at Violation severity,
    so a MolecularEntity can never be a process input directly. The selection
    follows that second hop rather than the crate being remodelled around it.
    """

    def test_a_compound_used_by_a_drawn_exposure_is_selected(self, crate):
        selected = _select_processes(crate)
        assert "#compound-a1" in selected, sorted(selected)
        assert "#compound-a2" in selected, sorted(selected)

    def test_the_compound_arrives_with_the_table_that_links_it(self, crate):
        """A compound with no edge to any work is the opposite of the point."""
        selected = _select_processes(crate)
        assert "#conditions-a" in selected

    def test_a_compound_of_an_undrawn_protocol_stays_out(self, crate):
        """The rule is 'follow', not 'collect every reagent edge in the crate'."""
        selected = _select_processes(crate)
        assert "#spare-compound" not in selected, (
            "a compound whose protocol no drawn step executes has no place on a "
            "view about what the steps did"
        )
        assert "#spare-protocol" not in selected

    def test_the_exposure_sub_view_carries_them_too(self, crate):
        """The sub-view is the parent restricted to one discriminator (#624).

        It is the view a reader opens to inspect the exposure, so it is the one
        that most needs the compounds.
        """
        selected = _select_processes(crate, "Exposure")
        assert {"#compound-a1", "#compound-a2", "#compound-b1"} <= selected, sorted(selected)

    def test_a_readout_sub_view_draws_no_compound(self, crate):
        """Nothing in a readout executes a protocol carrying reagents."""
        selected = _select_processes(crate, "EndpointReadout")
        assert not {"#compound-a1", "#compound-a2", "#compound-b1"} & selected


class TestTheLaneDrawsOneAssay:
    """An assay is what produces a research object, so it is what the view draws.

    Scoped to one assay the closure is small and, since #678 gave each assay its
    own culture, nothing on the lane belongs to another.
    """

    def test_it_draws_the_whole_material_chain(self, crate):
        lane = _select_assay_lane(crate, "#assay-a")
        spine = {
            "#cellline-a",
            "#culture-a",
            "#cultured-a",
            "#exposure-a",
            "#exposed-a",
            "#readout-a",
            "raw/a1.csv",
            "raw/a2.csv",
            "#analysis-a",
            "processed/a.csv",
        }
        assert spine <= lane, sorted(spine - lane)

    def test_it_draws_the_protocol_under_each_step(self, crate):
        lane = _select_assay_lane(crate, "#assay-a")
        band = {
            "#culture-protocol-a",
            "#conditions-a",
            "#readout-protocol-a",
            "#analysis-protocol-a",
        }
        assert band <= lane, sorted(band - lane)

    def test_it_draws_the_compounds(self, crate):
        lane = _select_assay_lane(crate, "#assay-a")
        assert {"#compound-a1", "#compound-a2"} <= lane

    def test_no_node_of_another_assay_is_on_the_lane(self, crate):
        """The property that makes the lane readable at all."""
        lane = _select_assay_lane(crate, "#assay-a")
        strays = {i for i in lane if i.endswith("-b") or i.endswith("-b1")}
        assert not strays, f"assay B bled onto assay A's lane: {sorted(strays)}"

    def test_the_study_and_investigation_are_not_drawn(self, crate):
        """The lane is the assay. A container reaches every step in it."""
        lane = _select_assay_lane(crate, "#assay-a")
        assert "#study" not in lane
        assert "./" not in lane

    def test_the_assay_itself_is_the_frame_not_a_node(self, crate):
        """Drawn, it would connect to every step and reproduce the star this
        whole change removes."""
        lane = _select_assay_lane(crate, "#assay-a")
        assert "#assay-a" not in lane, "the lane is named for the assay, not drawn from it"

    def test_the_other_lane_is_the_other_assay(self, crate):
        """Not a tautology against a fixture with one assay: B is a real chain."""
        lane = _select_assay_lane(crate, "#assay-b")
        assert {"#cellline-b", "#culture-b", "#exposure-b", "#compound-b1"} <= lane
        assert "#cellline-a" not in lane

    def test_an_id_that_is_not_an_assay_draws_nothing(self, crate):
        assert _select_assay_lane(crate, "#study") == set()
        assert _select_assay_lane(crate, "#nope") == set()


class TestTheLanesAreMintedFromTheCrate:
    """As many lanes as the crate has assays — one, four, or none.

    They are not declared anywhere: a deposit's assays are its own, and a lane
    with nothing to draw is dropped for the same reason a view with no members
    is, because an empty chip is a promise the canvas cannot keep.
    """

    def test_there_is_one_lane_per_assay(self, lanes):
        assert len(lanes) == 2, [lane["key"] for lane in lanes]

    def test_each_is_labelled_with_the_assay_name(self, lanes):
        labels = {lane["label"] for lane in lanes}
        assert labels == {"Deiodinase assay", "TH transport assay"}

    def test_a_lane_names_the_assay_it_was_minted_from(self, lanes):
        assert {lane["assay"] for lane in lanes} == {"#assay-a", "#assay-b"}

    def test_the_keys_carry_no_comma(self, lanes):
        """The key is what a shared link would carry, and a comma is the
        separator the explorer's own hash uses."""
        for lane in lanes:
            assert "," not in lane["key"]
            assert lane["key"] == lane["key"].strip()

    def test_the_keys_are_distinct(self, lanes):
        keys = [lane["key"] for lane in lanes]
        assert len(keys) == len(set(keys)), keys

    def test_the_members_are_sorted_and_known_to_the_model(self, lanes, crate):
        for lane in lanes:
            assert lane["members"] == sorted(lane["members"])
            assert set(lane["members"]) <= crate.known

    def test_a_crate_with_no_assay_mints_no_lane(self):
        assert build_assay_lanes(tabbed_views_graph()) == []

    def test_an_assay_whose_steps_the_crate_never_states_is_dropped(self):
        """The lane would be a heading over an empty drawing, which says less
        than leaving it out and is easy to read as a rendering failure."""
        graph = assay_lane_graph()
        for entity in graph["@graph"]:
            if entity.get("@id") == "#assay-a":
                entity.pop("about", None)
        keys = [lane["key"] for lane in build_assay_lanes(graph)]
        assert keys == ["assay-th-transport-assay"], keys

    def test_the_lane_is_not_a_view_of_the_entity_explorer(self, payload):
        """It was a sub-row under the Assays chip, and combining it with any
        other view handed the lane's geometry a graph it had no place for. The
        two are separate sections now, and the explorer offers no lane."""
        assert not any(view.get("parent") == "assays" for view in payload["views"])
        assert all("lane" not in view for view in payload["views"])

    def test_the_assays_chip_still_counts_only_assays(self, payload):
        assays = next(v for v in payload["views"] if v["key"] == "assays")
        assert assays["count"] == 2


class TestTheLaneKeyIsReadableAndUnique:
    """The key is what a shared link carries, so it is built from the name.

    Real assay ids repeat their own kind (``#Assay_assay_deiodinase_assay``), so
    slugging the id yields ``assay-assay-assay-deiodinase-assay`` — unique, and
    unreadable in a URL. Names read well and are not guaranteed unique, so the
    name makes the key and the id settles ties.
    """

    def test_the_key_comes_from_the_name(self):
        keys = {lane["label"]: lane["key"] for lane in build_assay_lanes(assay_lane_graph())}
        assert keys["Deiodinase assay"] == "assay-deiodinase-assay"
        assert keys["TH transport assay"] == "assay-th-transport-assay"

    def test_an_id_that_repeats_its_kind_does_not_repeat_it_in_the_key(self):
        """The shape every assay in a built crate actually has."""
        graph = assay_lane_graph()
        for entity in graph["@graph"]:
            if entity.get("@id") == "#assay-a":
                entity["@id"] = "#Assay_assay_deiodinase_assay"
            for key in ("about", "hasPart"):
                refs = entity.get(key)
                if isinstance(refs, list):
                    for ref in refs:
                        if ref.get("@id") == "#assay-a":
                            ref["@id"] = "#Assay_assay_deiodinase_assay"
        keys = [lane["key"] for lane in build_assay_lanes(graph)]
        assert "assay-deiodinase-assay" in keys, keys

    def test_two_assays_of_one_name_still_get_one_key_each(self):
        """Names are not unique. A key that collided would make one lane
        unreachable and the other unlinkable."""
        graph = assay_lane_graph()
        for entity in graph["@graph"]:
            if entity.get("@id") == "#assay-b":
                entity["name"] = "Deiodinase assay"
        keys = [lane["key"] for lane in build_assay_lanes(graph)]
        assert len(keys) == len(set(keys)) == 2, keys
        assert all(k.startswith("assay-deiodinase-assay") for k in keys), keys

    def test_the_tie_break_does_not_depend_on_crate_order(self):
        """Two builds of one deposit must produce reports that diff to nothing,
        so a key may not depend on which assay the graph happened to list first.
        """
        graph = assay_lane_graph()
        for entity in graph["@graph"]:
            if entity.get("@id") == "#assay-b":
                entity["name"] = "Deiodinase assay"
        forward = {lane["label"] + lane["key"] for lane in build_assay_lanes(graph)}

        reversed_graph = dict(graph)
        reversed_graph["@graph"] = list(reversed(graph["@graph"]))
        backward = {lane["label"] + lane["key"] for lane in build_assay_lanes(reversed_graph)}
        assert forward == backward, (sorted(forward), sorted(backward))


class TestThePayloadCarriesTheLanes:
    """The lane section draws from the explorer's island, so the lanes ride in
    it — one crate document on a page that ships inside the crate is already the
    accepted cost, and a second copy for a second section would be another."""

    def test_the_island_carries_them(self, payload, lanes):
        assert payload["lanes"] == lanes

    def test_the_wire_format_indexes_a_lane_s_members_like_a_view_s(self, payload):
        """Encoded, not restated: the readable model is what Python consumers
        read, and the page carries the same thing small (#694)."""
        compact = _compact(payload)
        assert [lane["key"] for lane in compact["lanes"]] == [
            lane["key"] for lane in payload["lanes"]
        ]
        for lane in compact["lanes"]:
            assert all(isinstance(member, int) for member in lane["members"])
