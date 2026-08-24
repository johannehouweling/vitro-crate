"""One assay drawn as a lane, and the compounds that were a hop out of reach (#686).

The LabProcesses view draws 74 nodes for 15 steps on a real deposit, and its
Exposure sub-view draws none of the compounds — a researcher cannot visually
inspect the flow, which is what the view is for.

Two changes, tested here against the same crate:

* compounds join the process views, which they were missing by one hop rather
  than by a modelling gap;
* an assay becomes a selectable unit, drawing its own chain and nothing else.

Layout is a separate module and is tested in ``test_assay_lane_layout.py``
against the shipped JavaScript.
"""

from __future__ import annotations

import pytest

from builder.writers.entity_explorer import (
    _Crate,
    _select_assay_lane,
    _select_processes,
    build_explorer_payload,
)
from tests.fixtures.crate_graphs import assay_lane_graph

pytestmark = pytest.mark.timeout(180)


@pytest.fixture(scope="module")
def crate() -> _Crate:
    return _Crate(assay_lane_graph())


@pytest.fixture(scope="module")
def payload() -> dict:
    return build_explorer_payload(assay_lane_graph())


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


class TestTheLaneIsOfferedAsASubRow:
    """One sub-row per assay under the Assays chip — the pattern LabProcesses
    already uses for its four kinds. A child view narrows its parent (#624), so
    choosing one assay replaces the Assays selection and the containers drop out
    with it.
    """

    def _lanes(self, payload):
        return [v for v in payload["views"] if v.get("parent") == "assays"]

    def test_there_is_one_sub_row_per_assay(self, payload):
        lanes = self._lanes(payload)
        assert len(lanes) == 2, [v["key"] for v in lanes]

    def test_each_is_labelled_with_the_assay_name(self, payload):
        labels = {v["label"] for v in self._lanes(payload)}
        assert labels == {"Deiodinase assay", "TH transport assay"}

    def test_the_keys_survive_a_url_hash(self, payload):
        """View keys are joined with commas into the location hash, so a key
        carrying one would split into two views that do not exist."""
        for view in self._lanes(payload):
            assert "," not in view["key"]
            assert view["key"] == view["key"].strip()

    def test_the_keys_are_distinct(self, payload):
        keys = [v["key"] for v in self._lanes(payload)]
        assert len(keys) == len(set(keys)), keys

    def test_the_count_is_what_the_lane_draws(self, payload):
        """The view's name covers everything it draws, so `subject` is unset and
        the count is the membership (#625)."""
        for view in self._lanes(payload):
            assert view["count"] == len(view["members"])

    def test_a_lane_is_not_on_by_default(self, payload):
        assert not any(v["default"] for v in self._lanes(payload))

    def test_the_parent_chip_still_counts_only_assays(self, payload):
        """Adding children must not change what the parent is named for."""
        assays = next(v for v in payload["views"] if v["key"] == "assays")
        assert assays["count"] == 2


class TestTheLaneKeyIsReadableAndUnique:
    """The key is what a shared link carries, so it is built from the name.

    Real assay ids repeat their own kind (``#Assay_assay_deiodinase_assay``), so
    slugging the id yields ``assay-assay-assay-deiodinase-assay`` — unique, and
    unreadable in a URL. Names read well and are not guaranteed unique, so the
    name makes the key and the id settles ties.
    """

    def _views(self, graph):
        return [v for v in build_explorer_payload(graph)["views"] if v.get("parent") == "assays"]

    def test_the_key_comes_from_the_name(self):
        graph = assay_lane_graph()
        keys = {v["label"]: v["key"] for v in self._views(graph)}
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
        keys = [v["key"] for v in self._views(graph)]
        assert "assay-deiodinase-assay" in keys, keys

    def test_two_assays_of_one_name_still_get_one_key_each(self):
        """Names are not unique. A key that collided would make one lane
        unreachable and the other unlinkable."""
        graph = assay_lane_graph()
        for entity in graph["@graph"]:
            if entity.get("@id") == "#assay-b":
                entity["name"] = "Deiodinase assay"
        views = self._views(graph)
        keys = [v["key"] for v in views]
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
        forward = {v["label"] + v["key"] for v in self._views(graph)}

        reversed_graph = dict(graph)
        reversed_graph["@graph"] = list(reversed(graph["@graph"]))
        backward = {v["label"] + v["key"] for v in self._views(reversed_graph)}
        assert forward == backward, (sorted(forward), sorted(backward))
