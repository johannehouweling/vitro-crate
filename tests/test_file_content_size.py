"""Every File states its size if anything in the crate knows it.

The size used to depend on HOW a file entered the crate. The scanned-file loop
set `contentSize` from the scan; the drafted-File loop emitted only the entity's
own fields; and a file that was BOTH drafted and scanned took the drafted path and
was skipped by the scanned one as already covered — so it carried no size at all.

Two exports of one session, two hours apart, disagreed on exactly this: the
earlier crate had `contentSize` on its input workbooks and the later one did not,
because the files had been attached a different way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.state import CrateState, FileClassification
from builder.tools._crate_mapping import _known_file_size
from builder.tools.drafters import draft_investigation


def _file_entity(state, entity_id, **fields):
    from builder.state import Entity

    ent = Entity(entity_id=entity_id, type="File", fields=fields)
    state.add_entity(ent)
    return ent


@pytest.fixture
def state():
    st = CrateState()
    draft_investigation(st, {"name": "I", "description": "D"})
    return st


class TestWhereTheSizeComesFrom:
    def test_the_entity_field_wins(self, state):
        fe = _file_entity(state, "file_x", dest_path="data/x.csv", contentSize="4242")
        assert _known_file_size(state, fe, None) == 4242

    def test_the_scan_is_used_when_the_entity_has_none(self, state):
        """The scan already measured it; the size costs nothing to state."""
        state.scanned_files.append(
            FileClassification(path="/in/x.csv", filename="x.csv", size=999, mime_type="text/csv")
        )
        fe = _file_entity(state, "file_x", dest_path="data/x.csv")
        assert _known_file_size(state, fe, None) == 999

    def test_the_file_on_disk_is_used_as_a_last_resort(self, state, tmp_path):
        src = tmp_path / "real.csv"
        src.write_text("a,b\n1,2\n", encoding="utf-8")
        fe = _file_entity(state, "file_real", dest_path="real.csv", path=str(src))
        assert _known_file_size(state, fe, str(tmp_path)) == src.stat().st_size

    def test_a_malformed_stored_value_is_not_propagated(self, state):
        state.scanned_files.append(
            FileClassification(path="/in/x.csv", filename="x.csv", size=77, mime_type="text/csv")
        )
        fe = _file_entity(state, "file_x", dest_path="data/x.csv", contentSize="not-a-number")
        assert _known_file_size(state, fe, None) == 77

    def test_nothing_known_states_nothing(self, state):
        """A synthesized placeholder describes no bytes; silence is correct."""
        fe = _file_entity(state, "file_ghost", dest_path="data/ghost.csv")
        assert _known_file_size(state, fe, None) is None


class TestUndepositedOutputPlaceholders:
    """A step whose output the deposit does not contain (#438, #589).

    The placeholder used to be sized from the header line the build was about to
    write. There is no header now — the file is created empty — so the size is a
    fact rather than a prediction, and the in-memory and written crates still
    agree about it.
    """

    def test_a_placeholder_for_an_undeposited_output_is_empty(self, state):
        fe = _file_entity(state, "file_prov", dest_path="data/p.csv", provisional=True)

        assert _known_file_size(state, fe, None) == 0

    def test_the_size_matches_the_file_the_build_actually_writes(self, tmp_path, state):
        """The whole point of sizing in memory: both crates say the same thing."""
        from builder.tools._crate_mapping import _materialize_missing_output

        fe = _file_entity(state, "file_prov", dest_path="data/p.csv", provisional=True)
        written = _materialize_missing_output(fe, tmp_path, "data/p.csv")

        assert Path(written).stat().st_size == _known_file_size(state, fe, None)


class TestItReachesTheGraph:
    def test_a_drafted_and_scanned_file_carries_a_size(self, state):
        """The regression case: drafted AND scanned, so neither loop set it."""
        from builder.tools.builder import assemble_crate

        state.scanned_files.append(
            FileClassification(
                path="/in/book.xlsx", filename="book.xlsx", size=32959, mime_type="application/xlsx"
            )
        )
        _file_entity(state, "file_book", dest_path="book.xlsx", name="book.xlsx")
        graph = assemble_crate(
            state, output_dir=None, materialize_payload=False, include_all_scanned=True
        ).metadata.generate()["@graph"]
        node = next(n for n in graph if str(n.get("@id", "")).endswith("book.xlsx"))
        assert node.get("contentSize") == "32959"
