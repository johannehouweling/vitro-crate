"""Exporting a crate nothing has changed is the same crate.

A profiled session called `export_crate` 32 times, 29 of them immediately after
a `build_and_validate`, and spent 279 of its 305 export seconds rewriting a
crate byte for byte. `build_and_validate` already assembles in memory and
touches no disk, so exporting to check a verdict buys nothing.

This is a memo rather than a prompt rule because it does not depend on the agent
behaving. What it must never do is claim a reuse that leaves the user without
their crate, so the interesting tests here are the ones about NOT reusing:

* any change the written crate could differ by busts it;
* a different destination is a different result;
* a directory deleted since is written again;
* a failed export is never remembered as a completed one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools import builder as builder_mod
from builder.tools.builder import export_crate


@pytest.fixture(autouse=True)
def _clear_memo():
    builder_mod._EXPORT_MEMO.clear()
    yield
    builder_mod._EXPORT_MEMO.clear()


def _state(title: str = "A crate") -> CrateState:
    state = CrateState()
    state.metadata.title = title
    state.metadata.description = "Enough to build."
    return state


def _export(state: CrateState, path) -> dict:
    return export_crate(state, str(path), validate=False)


class TestTheSecondExportIsFree:
    def test_an_unchanged_export_is_reused(self, tmp_path):
        state = _state()
        first = _export(state, tmp_path)
        second = _export(state, tmp_path)
        assert first.get("reused", False) is False, "the first export must actually write"
        assert second["reused"] is True

    def test_the_reused_result_matches_the_written_one(self, tmp_path):
        """A caller must not be able to tell, apart from the flag."""
        state = _state()
        first = _export(state, tmp_path)
        second = _export(state, tmp_path)
        assert second["crate_path"] == first["crate_path"]
        assert second["success"] is first["success"] is True

    def test_the_crate_is_still_on_disk(self, tmp_path):
        state = _state()
        _export(state, tmp_path)
        _export(state, tmp_path)
        assert (tmp_path / "ro-crate-metadata.json").is_file()


class TestAnythingThatCouldDifferBustsIt:
    def test_a_metadata_edit_re_exports(self, tmp_path):
        state = _state()
        _export(state, tmp_path)
        state.metadata.title = "A different crate"
        assert _export(state, tmp_path).get("reused", False) is False

    def test_a_new_entity_re_exports(self, tmp_path):
        state = _state()
        _export(state, tmp_path)
        entity = Entity(
            entity_id="sample_1", type="Sample", _provenance=EntityProvenance(created_by="llm")
        )
        entity.set_fields_from_dict({"name": "A sample"}, source="llm")
        state.add_entity(entity)
        assert _export(state, tmp_path).get("reused", False) is False

    def test_a_second_destination_within_a_session_is_redirected(self, tmp_path):
        """Deliberate change: a session improves ONE crate.

        The first export establishes where this session's crate lives; a later
        call naming somewhere else is editing the same crate, not starting a
        second one. Writing both would leave two copies that immediately
        disagree — which is how one session ended up with twelve.
        """
        state = _state()
        _export(state, tmp_path / "one")
        state.metadata.title = "Now different"
        result = _export(state, tmp_path / "two")
        assert result["crate_path"] == str(tmp_path / "one")
        assert not (tmp_path / "two").exists()

    def test_asking_for_different_contents_re_exports(self, tmp_path):
        state = _state()
        export_crate(state, str(tmp_path), validate=False)
        result = export_crate(state, str(tmp_path), validate=False, embed_report=False)
        assert result.get("reused", False) is False


class TestItNeverLeavesTheUserWithoutACrate:
    def test_a_deleted_output_is_written_again(self, tmp_path):
        """The memo knows what we did, not what happened to the directory."""
        state = _state()
        target = tmp_path / "crate"
        _export(state, target)
        shutil.rmtree(target)
        result = _export(state, target)
        assert result.get("reused", False) is False
        assert (target / "ro-crate-metadata.json").is_file()

    def test_an_emptied_output_is_written_again(self, tmp_path):
        state = _state()
        target = tmp_path / "crate"
        _export(state, target)
        (target / "ro-crate-metadata.json").unlink()
        _export(state, target)
        assert (target / "ro-crate-metadata.json").is_file()

    def test_a_failed_export_is_not_remembered(self, tmp_path, monkeypatch):
        """Otherwise the next call reports a crate that was never written."""
        state = _state()

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(builder_mod, "assemble_crate", boom)
        failed = _export(state, tmp_path)
        assert failed["success"] is False
        assert builder_mod._EXPORT_MEMO == {}

    def test_an_unfingerprintable_state_just_exports(self, tmp_path, monkeypatch):
        """No key, no memo — never a silent skip."""
        state = _state()

        def boom(self):
            raise RuntimeError("cannot hash")

        monkeypatch.setattr(CrateState, "validation_fingerprint", boom)
        assert _export(state, tmp_path)["success"] is True
        assert _export(state, tmp_path).get("reused", False) is False


class TestTheMemoStaysSmall:
    def test_it_is_bounded(self, tmp_path):
        """An agent loop re-exports one crate; the cap is a backstop."""
        for n in range(builder_mod._EXPORT_MEMO_MAX + 3):
            _export(_state(f"Crate {n}"), tmp_path / f"c{n}")
        assert len(builder_mod._EXPORT_MEMO) <= builder_mod._EXPORT_MEMO_MAX


class TestOneSessionWritesOneCrate:
    """A session improves ONE crate, so each export supersedes the last.

    A profiled session exported 32 times to TWELVE directories (…_crate_v64 …
    _v75, one labelled "svhps26_complete_validated_v68") because the agent
    minted a fresh versioned path per export. That left twelve complete copies
    on disk — `output/` had reached 75 crates and 367 MB — defeated the loop's
    export guard, which only applies when no output_path is given, and made
    every export a first export.

    The user's destination still decides WHERE: `--output` and `--resume` set it
    before the loop starts, and a previous session's build is never clobbered.
    What is refused is an agent inventing a new destination mid-run for a crate
    it is editing.
    """

    def test_an_agent_supplied_path_is_redirected(self, tmp_path):
        state = _state()
        state.metadata.output_path = str(tmp_path / "crate_v64")
        _export(state, tmp_path / "crate_v64")
        state.metadata.title = "Now different"
        result = export_crate(state, str(tmp_path / "crate_v65"), validate=False)
        assert result["crate_path"] == str(tmp_path / "crate_v64")

    def test_only_one_directory_is_created(self, tmp_path):
        state = _state()
        state.metadata.output_path = str(tmp_path / "crate_v64")
        for n, title in enumerate(("one", "two", "three")):
            state.metadata.title = title
            export_crate(state, str(tmp_path / f"crate_v6{n + 5}"), validate=False)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["crate_v64"]

    def test_the_redirect_is_reported(self, tmp_path):
        """Otherwise the agent concludes its crate is where it asked."""
        state = _state()
        state.metadata.output_path = str(tmp_path / "crate_v64")
        _export(state, tmp_path / "crate_v64")
        state.metadata.title = "Now different"
        result = export_crate(state, str(tmp_path / "crate_v65"), validate=False)
        assert "crate_v64" in result["note"]
        assert "crate_v65" in result["note"]

    def test_a_reused_export_reports_it_too(self, tmp_path):
        state = _state()
        state.metadata.output_path = str(tmp_path / "crate_v64")
        _export(state, tmp_path / "crate_v64")
        result = export_crate(state, str(tmp_path / "crate_v65"), validate=False)
        assert result["reused"] is True
        assert "nothing was written" in result["note"]

    def test_the_users_destination_is_honoured(self, tmp_path):
        """`--output` sets it before the loop; that choice is not overridden."""
        state = _state()
        state.metadata.output_path = str(tmp_path / "chosen")
        result = _export(state, tmp_path / "chosen")
        assert result["crate_path"] == str(tmp_path / "chosen")
        assert "note" not in result

    def test_with_no_established_destination_the_path_is_used(self, tmp_path):
        """Nothing to redirect to — the first export decides."""
        state = _state()
        result = export_crate(state, str(tmp_path / "first"), validate=False)
        assert result["crate_path"] == str(tmp_path / "first")
        assert (tmp_path / "first" / "ro-crate-metadata.json").is_file()
