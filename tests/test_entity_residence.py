"""Where an entity's bytes live, which `status` was never able to say (#687).

``build_crate_graph`` marks **every entity described in the metadata** with one
status, so a Cellosaurus IRI, a ``#fragment`` PropertyValue and a PDF on disk all
carried the same value while ``external`` / ``dangling`` were reserved for ids
that are referenced and never described.

That is a fine answer to "does the crate describe this id?" and no answer at all
to "are the bytes here?". Residence is the second, orthogonal fact, read off the
``@id`` with no heuristics — and it is about to be load-bearing, because a tint
driven off `status` would paint a compound as if its bytes were in the crate.
"""

from __future__ import annotations

import pytest

from builder.writers.provenance_dag import build_crate_graph

pytestmark = pytest.mark.timeout(180)


def _graph() -> dict:
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "An investigation",
                "hasPart": [{"@id": "data/raw.csv"}, {"@id": "#compound"}],
                "mentions": [{"@id": "https://www.wikidata.org/wiki/Q42"}],
                "author": [{"@id": "#nobody"}],
            },
            {
                "@id": "data/raw.csv",
                "@type": "File",
                "name": "raw.csv",
                "encodingFormat": "text/csv",
            },
            {
                "@id": "#compound",
                "@type": "MolecularEntity",
                "name": "Amiodarone",
            },
            {
                "@id": "https://www.wikidata.org/wiki/Q42",
                "@type": "DefinedTerm",
                "name": "A term described here and living elsewhere",
            },
        ]
    }


def _nodes() -> dict[str, dict]:
    return {n["id"]: n for n in build_crate_graph(_graph(), all_edges=True)["nodes"]}


class TestResidenceIsReadOffTheId:
    def test_a_relative_path_is_carried(self):
        """The one case where bytes are in the crate directory."""
        assert _nodes()["data/raw.csv"]["residence"] == "carried"

    def test_a_fragment_is_a_record(self):
        """A description with no bytes. The case a tint must not paint."""
        assert _nodes()["#compound"]["residence"] == "record"

    def test_an_absolute_iri_lives_elsewhere(self):
        """Described in this crate, resolvable, and not here."""
        assert _nodes()["https://www.wikidata.org/wiki/Q42"]["residence"] == "elsewhere"

    def test_an_id_nothing_describes_is_only_named(self):
        assert _nodes()["#nobody"]["residence"] == "named"

    def test_the_crate_root_is_carried(self):
        """`./` is the crate directory itself."""
        assert _nodes()["./"]["residence"] == "carried"

    def test_every_node_has_one(self):
        assert all(n.get("residence") for n in _nodes().values())


class TestResidenceIsNotStatus:
    """The two are orthogonal, and conflating them is the defect."""

    def test_three_entities_share_a_status_and_differ_in_residence(self):
        nodes = _nodes()
        described = ["data/raw.csv", "#compound", "https://www.wikidata.org/wiki/Q42"]
        assert {nodes[i]["status"] for i in described} == {"described"}
        assert len({nodes[i]["residence"] for i in described}) == 3

    def test_status_says_described_not_in_crate(self):
        """`in_crate` was read as "the bytes are here", which it never meant.

        It answers "does the crate describe this id?" — so it says that.
        """
        assert _nodes()["#compound"]["status"] == "described"
        assert not any(n["status"] == "in_crate" for n in _nodes().values())

    def test_the_other_two_statuses_are_unchanged(self):
        """Only the misleading value is renamed; `external` and `dangling`
        already said what they meant."""
        nodes = _nodes()
        assert nodes["#nobody"]["status"] == "dangling"


class TestThePayloadCarriesIt:
    """The browser needs residence to tint payload without claiming a compound's
    bytes are in the crate (#688). The field travels; nothing draws it yet.
    """

    def _payload(self) -> dict:
        from builder.writers.entity_explorer import build_explorer_payload

        return build_explorer_payload(_graph())

    def test_every_node_carries_its_residence(self):
        nodes = self._payload()["nodes"]
        assert nodes and all(n.get("residence") for n in nodes)

    def test_the_values_survive_the_trip(self):
        by_id = {n["id"]: n for n in self._payload()["nodes"]}
        assert by_id["data/raw.csv"]["residence"] == "carried"
        assert by_id["#compound"]["residence"] == "record"
        assert by_id["https://www.wikidata.org/wiki/Q42"]["residence"] == "elsewhere"
        assert by_id["#nobody"]["residence"] == "named"

    def test_status_and_residence_both_travel(self):
        """Two fields, because they are two facts."""
        by_id = {n["id"]: n for n in self._payload()["nodes"]}
        compound = by_id["#compound"]
        assert compound["status"] == "described"
        assert compound["residence"] == "record"

    def test_the_payload_version_moved(self):
        """A stale cached script reading `in_crate` must be loud, not silently
        wrong: every node's status value changed meaning-for-name."""
        from builder.writers.entity_explorer import PAYLOAD_VERSION

        assert self._payload()["version"] == PAYLOAD_VERSION
        assert PAYLOAD_VERSION >= 3
