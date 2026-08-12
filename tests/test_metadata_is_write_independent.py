"""What the agent validates must be what the crate ships.

`build_and_validate` assembles with `materialize_payload=False` and validates the
result; `export_crate` assembles with True and writes it. Whenever a leaf site
decided what METADATA to emit based on that flag, the agent spent its whole loop
validating a graph that differed from the crate it would produce — and the
difference only appeared after export, when the loop that could have acted on it
had finished.

Three landed that way before this was pinned: the provisional tables' description
and co-type, `contentSize`, and the CSVW schemas — nine nodes the in-loop
validation never saw, carrying what was historically the project's largest
finding bucket.

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
        # A provisional table, because that is where every divergence so far has
        # lived. Without one the comparison below passes vacuously — there is
        # nothing in the crate whose metadata the write flag could have changed.
        state.add_entity(
            Entity(
                entity_id="file_prov",
                type="File",
                fields={
                    "dest_path": "data/p.csv",
                    "name": "p.csv",
                    "provisional": True,
                    "table_kind": "measurements",
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


class TestTheColumnsAreDeclared:
    def test_a_provisional_table_declares_its_schema_in_memory(self):
        """We generate these columns, so the crate can declare them either way."""
        state = CrateState()
        draft_investigation(state, {"name": "T", "description": "D"})
        from builder.state import Entity

        state.add_entity(
            Entity(
                entity_id="file_prov",
                type="File",
                fields={
                    "dest_path": "data/p.csv",
                    "name": "p.csv",
                    "provisional": True,
                    "table_kind": "measurements",
                },
            )
        )
        graph = assemble_crate(
            state, output_dir=None, materialize_payload=False
        ).metadata.generate()["@graph"]
        schemas = [n for n in graph if "csvw:Schema" in str(n.get("@type"))]
        columns = [n for n in graph if "csvw:Column" in str(n.get("@type"))]
        assert schemas, "no CSVW schema without writing the payload"
        assert columns, "no CSVW columns without writing the payload"
        table = next(n for n in graph if str(n.get("@id", "")).endswith("p.csv"))
        assert table.get("tableSchema"), "the table does not point at its schema"
