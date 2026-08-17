"""What the agent validates must be what the crate ships.

`build_and_validate` assembles with `materialize_payload=False` and validates the
result; `export_crate` assembles with True and writes it. Whenever a leaf site
decided what METADATA to emit based on that flag, the agent spent its whole loop
validating a graph that differed from the crate it would produce — and the
difference only appeared after export, when the loop that could have acted on it
had finished.

Three landed that way before this was pinned: the placeholders' description and
co-type, `contentSize`, and the CSVW schemas — nine nodes the in-loop validation
never saw, carrying what was historically the project's largest finding bucket.
The schemas have since been removed outright (#589); what is left is still held
to the same rule, and the removal is pinned on both paths so it cannot return on
one of them.

The flag means "resolve a source path so ro-crate-py copies the payload". It does
not mean "should this metadata exist". This test is what keeps those apart.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from builder.state import CrateState
from builder.tools.builder import assemble_crate
from builder.tools.drafters import draft_investigation


def _graph(state_factory, materialize, output_dir):
    crate = assemble_crate(
        state_factory(),
        output_dir=output_dir,
        materialize_payload=materialize,
        include_all_scanned=True,
    )
    return {
        node["@id"]: {k: v for k, v in node.items() if k != "@id"}
        for node in crate.metadata.generate()["@graph"]
    }


@pytest.fixture(scope="module")
def graphs():
    """The same state assembled both ways."""

    def factory():
        from builder.state import Entity

        state = CrateState()
        draft_investigation(state, {"name": "T", "description": "D"})
        # A File carrying the legacy `provisional` flag, because that is where
        # every divergence so far has lived. Without one the comparison below
        # passes vacuously — there is nothing in the crate whose metadata the
        # write flag could have changed.
        state.add_entity(
            Entity(
                entity_id="file_prov",
                type="File",
                fields={
                    "dest_path": "data/p.csv",
                    "name": "p.csv",
                    "provisional": True,
                },
            )
        )
        return state

    with tempfile.TemporaryDirectory() as td:
        written = _graph(factory, True, Path(td))
        in_memory = _graph(factory, False, None)
    return written, in_memory


class TestTheGraphsAgree:
    def test_no_node_is_missing_from_the_validated_graph(self, graphs):
        """The failure that hid nine CSVW nodes: memory was a strict SUBSET."""
        written, in_memory = graphs
        missing = sorted(set(written) - set(in_memory))
        assert not missing, f"only in the written crate, so never validated: {missing}"

    def test_no_node_is_invented_by_the_validated_graph(self, graphs):
        written, in_memory = graphs
        extra = sorted(set(in_memory) - set(written))
        assert not extra, f"validated but never written: {extra}"

    def test_every_shared_node_carries_the_same_properties(self, graphs):
        """`contentSize` and the provisional description both failed here."""
        written, in_memory = graphs
        differing = {
            nid: sorted(set(written[nid]) ^ set(in_memory[nid]))
            for nid in set(written) & set(in_memory)
            if set(written[nid]) ^ set(in_memory[nid])
        }
        assert not differing, f"property sets differ between the two paths: {differing}"


class TestALegacyPlaceholderExportsClean:
    """`provisional` is no longer written, but old sessions still carry it (#592).

    A step with no deposited output now gets no File at all, so there is no
    placeholder left to keep in step across the two paths. What remains is the
    compatibility case: a session saved before the change holds File entities
    still flagged `provisional`, and resuming one must not emit that flag into
    the crate as a stray literal — on either path.
    """

    @staticmethod
    def _graph_both_ways():
        from builder.state import Entity

        def factory():
            state = CrateState()
            draft_investigation(state, {"name": "T", "description": "D"})
            state.add_entity(
                Entity(
                    entity_id="file_legacy",
                    type="File",
                    fields={
                        "dest_path": "data/p.csv",
                        "name": "p.csv",
                        "provisional": True,
                    },
                )
            )
            return state

        with tempfile.TemporaryDirectory() as td:
            written = assemble_crate(
                factory(), output_dir=Path(td), materialize_payload=True
            ).metadata.generate()["@graph"]
            in_memory = assemble_crate(
                factory(), output_dir=None, materialize_payload=False
            ).metadata.generate()["@graph"]
        return written, in_memory

    def test_the_legacy_flag_never_reaches_the_crate(self):
        for graph in self._graph_both_ways():
            node = next(n for n in graph if str(n.get("@id", "")).endswith("p.csv"))
            assert "provisional" not in node, node

    def test_neither_path_declares_a_column_contract_for_it(self):
        for graph in self._graph_both_ways():
            assert [n for n in graph if "csvw:Schema" in str(n.get("@type"))] == []
            assert [n for n in graph if "csvw:Column" in str(n.get("@type"))] == []

    def test_neither_path_types_it_as_a_table(self):
        for graph in self._graph_both_ways():
            node = next(n for n in graph if str(n.get("@id", "")).endswith("p.csv"))
            assert "csvw:Table" not in str(node.get("@type")), node
