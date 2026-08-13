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

    def test_a_second_destination_is_a_different_export(self, tmp_path):
        """`export_crate`'s contract: an explicit path is honoured.

        Keeping ONE crate per session is the AGENT's constraint and is enforced
        in the loop, not here — the CLI and library callers rely on an explicit
        argument winning.
        """
        state = _state()
        _export(state, tmp_path / "one")
        result = _export(state, tmp_path / "two")
        assert result.get("reused", False) is False
        assert (tmp_path / "two" / "ro-crate-metadata.json").is_file()

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


class TestTheAgentKeepsToOneCratePerSession:
    """The agent must not invent a destination mid-run for a crate it is editing.

    A profiled session exported 32 times to TWELVE directories (…_crate_v64 …
    _v75, one save labelled "svhps26_complete_validated_v68") because the model
    minted a fresh versioned path per export. Each is a complete copy —
    `output/` had reached 75 crates and 367 MB — and naming a path also stepped
    around the loop's unchanged-crate guard, which only applies when none is
    given.

    Enforced in the LOOP, not in `export_crate`: that function's contract is
    that an explicit argument wins, and the CLI and library callers rely on it.
    A human passing `--output` still decides where the crate lives.
    """

    def _tool(self, engine, name="export_crate"):
        from builder.agents.react.agent_loop import _build_langchain_tools

        return next(t for t in _build_langchain_tools(engine) if t.name == name)

    def _engine(self, destination):
        from builder.engine import AgentEngine

        engine = AgentEngine()
        engine.state = _state()
        engine.state.metadata.output_path = str(destination)
        return engine

    def test_a_path_the_agent_invents_is_redirected(self, tmp_path, monkeypatch):
        seen: list = []
        engine = self._engine(tmp_path / "crate_v64")
        monkeypatch.setattr(
            engine, "run_tool", lambda name, **kw: seen.append(kw.get("output_path")) or {}
        )
        self._tool(engine).func(output_path=str(tmp_path / "crate_v65"))
        assert seen == [str(tmp_path / "crate_v64")]

    def test_the_established_destination_is_left_alone(self, tmp_path, monkeypatch):
        seen: list = []
        engine = self._engine(tmp_path / "crate_v64")
        monkeypatch.setattr(
            engine, "run_tool", lambda name, **kw: seen.append(kw.get("output_path")) or {}
        )
        self._tool(engine).func(output_path=str(tmp_path / "crate_v64"))
        assert seen == [str(tmp_path / "crate_v64")]

    def test_with_no_established_destination_nothing_is_redirected(self, tmp_path, monkeypatch):
        seen: list = []
        engine = self._engine("")
        engine.state.metadata.output_path = None
        monkeypatch.setattr(
            engine, "run_tool", lambda name, **kw: seen.append(kw.get("output_path")) or {}
        )
        self._tool(engine).func(output_path=str(tmp_path / "first"))
        assert seen == [str(tmp_path / "first")]
