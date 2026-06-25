"""Build-path root date wiring: releaseDate / dateModified (#180).

The gold S-VHPS21 root carries ``releaseDate`` and ``dateModified`` in addition
to the auto-set ``datePublished``. ``CrateMetadata`` gains ``release_date`` /
``date_modified`` so those can be expressed, and the deterministic build path
(`populate_crate`) emits them on the Root Data Entity when set — without ever
overriding ro-crate-py's auto-set ``datePublished`` (D5: only set what is
passed). All assertions read the assembled ``@graph`` (no disk write).
"""

from __future__ import annotations

from rocrate.rocrate import ROCrate

from builder.state import CrateState
from builder.tools._crate_mapping import populate_crate
from profiles.context import ISA_TOX_CONTEXT


def _graph(state: CrateState) -> list[dict]:
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, None, materialize_payload=False, include_all_scanned=False)
    return crate.metadata.generate()["@graph"]


def _root(state: CrateState) -> dict:
    graph = _graph(state)
    root = next((n for n in graph if n.get("@id") == "./"), None)
    assert root is not None, "the crate must have a Root Data Entity (./)"
    return root


class TestRootDatesEmitted:
    def _state(self) -> CrateState:
        state = CrateState()
        state.metadata.title = "Dated investigation"
        state.metadata.release_date = "2025-11-10"
        state.metadata.date_modified = "2026-06-14T19:37:30Z"
        return state

    def test_release_date_emitted_on_root(self):
        root = _root(self._state())
        assert root.get("releaseDate") == "2025-11-10"

    def test_date_modified_emitted_on_root(self):
        root = _root(self._state())
        assert root.get("dateModified") == "2026-06-14T19:37:30Z"

    def test_auto_date_published_preserved(self):
        # Setting releaseDate/dateModified must NOT clobber ro-crate-py's
        # auto-set datePublished — all three coexist on the gold root.
        root = _root(self._state())
        assert root.get("datePublished"), (
            "ro-crate-py's auto-set datePublished must survive date wiring"
        )


class TestRootDatesAbsent:
    def test_absent_dates_not_emitted(self):
        # An undated crate must not grow a fabricated releaseDate/dateModified.
        state = CrateState()
        state.metadata.title = "Undated investigation"
        root = _root(state)
        assert "releaseDate" not in root
        assert "dateModified" not in root

    def test_auto_date_published_still_set_when_dates_absent(self):
        state = CrateState()
        state.metadata.title = "Undated investigation"
        root = _root(state)
        assert root.get("datePublished"), (
            "datePublished is auto-set by ro-crate-py regardless of our date fields"
        )
