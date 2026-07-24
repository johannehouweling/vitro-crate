"""Regression tests for crate output location and payload copying (#128).

Two bugs:
1. `export_crate` ignored `state.metadata.output_path` (the user's folder) and
   always wrote to `sessions/<id>/working_crate/`.
2. File entities were added with `source=None`, so `ro-crate-py` never copied the
   referenced data files into the crate (the `No source for …` warnings).
"""

from __future__ import annotations

import warnings
from pathlib import Path

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate


def _state_with_output(tmp_path: Path) -> CrateState:
    state = CrateState()
    state.session_id = "sess-128"
    state.metadata.output_path = str(tmp_path / "user_chosen")
    return state


class TestOutputLocation:
    """export_crate must honor state.metadata.output_path (#128 bug 1)."""

    def test_defaults_to_state_output_path(self, tmp_path: Path) -> None:
        """build_crate with no explicit arg writes to the user's configured folder."""
        state = _state_with_output(tmp_path)

        result = build_crate(state)

        expected = tmp_path / "user_chosen"
        assert result["success"], result["error"]
        assert result["crate_path"] == str(expected)
        assert (expected / "ro-crate-metadata.json").is_file()

    def test_explicit_arg_overrides_state(self, tmp_path: Path) -> None:
        """An explicit output_path argument still wins over state.metadata."""
        state = _state_with_output(tmp_path)
        explicit = tmp_path / "explicit"

        result = build_crate(state, str(explicit))

        assert result["crate_path"] == str(explicit)
        assert (explicit / "ro-crate-metadata.json").is_file()

    def test_falls_back_to_session_default(self, tmp_path, monkeypatch) -> None:
        """With neither arg nor state.metadata.output_path, use the session default."""
        monkeypatch.chdir(tmp_path)
        # Assert the *default* session root convention (relative "sessions/"),
        # so clear the test harness's VITRO_SESSION_DIR isolation override.
        monkeypatch.delenv("VITRO_SESSION_DIR", raising=False)
        state = CrateState()
        state.session_id = "sess-fallback"
        # no output_path set

        result = build_crate(state)

        default = Path("sessions") / "sess-fallback" / "working_crate"
        assert result["crate_path"] == str(default)
        assert (tmp_path / default / "ro-crate-metadata.json").is_file()


class TestPayloadCopied:
    """File entities whose source resolves locally are copied into the crate (#128 bug 2)."""

    def test_local_file_copied_no_warning(self, tmp_path: Path) -> None:
        """A File referencing a real file under input_path is copied into the
        crate payload, and emits no 'No source' warning."""
        inp = tmp_path / "userdata"
        (inp / "data").mkdir(parents=True)
        (inp / "data" / "real.csv").write_text("a,b\n1,2\n", encoding="utf-8")

        out = tmp_path / "out"
        state = CrateState()
        state.session_id = "sess-payload"
        state.metadata.input_path = str(inp)
        state.metadata.output_path = str(out)
        state.add_entity(
            Entity(
                entity_id="f1",
                type="File",
                fields={"name": "real.csv", "path": "data/real.csv"},
                _provenance=EntityProvenance(created_by="scanner"),
            )
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = build_crate(state)

        assert result["success"], result["error"]
        copied = out / "data" / "real.csv"
        assert copied.is_file(), "payload file was not copied into the crate"
        assert copied.read_text(encoding="utf-8") == "a,b\n1,2\n"
        no_source = [str(w.message) for w in caught if "No source" in str(w.message)]
        assert not no_source, f"unexpected No-source warnings: {no_source}"

    def test_in_place_build_keeps_file_no_warning(self, tmp_path: Path) -> None:
        """Building the crate in place (output_path == input_path) must not crash
        (no SameFileError) and must not warn — the source already sits at dest."""
        root = tmp_path / "crate_root"
        (root / "data").mkdir(parents=True)
        (root / "data" / "real.csv").write_text("x\n1\n", encoding="utf-8")

        state = CrateState()
        state.session_id = "sess-inplace"
        state.metadata.input_path = str(root)
        state.metadata.output_path = str(root)  # in place
        state.add_entity(
            Entity(
                entity_id="f1",
                type="File",
                fields={"name": "real.csv", "path": "data/real.csv"},
                _provenance=EntityProvenance(created_by="scanner"),
            )
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = build_crate(state)

        assert result["success"], result["error"]
        assert (root / "data" / "real.csv").read_text(encoding="utf-8") == "x\n1\n"
        no_source = [str(w.message) for w in caught if "No source" in str(w.message)]
        assert not no_source, f"unexpected No-source warnings: {no_source}"

    def test_missing_local_file_does_not_crash(self, tmp_path: Path) -> None:
        """A File whose source can't be resolved on disk degrades gracefully
        (metadata-only reference) rather than crashing the build."""
        out = tmp_path / "out"
        state = CrateState()
        state.session_id = "sess-missing"
        state.metadata.input_path = str(tmp_path / "nope")
        state.metadata.output_path = str(out)
        state.add_entity(
            Entity(
                entity_id="f2",
                type="File",
                fields={"name": "ghost.csv", "path": "data/ghost.csv"},
                _provenance=EntityProvenance(created_by="scanner"),
            )
        )

        result = build_crate(state)

        assert result["success"], result["error"]
        assert (out / "ro-crate-metadata.json").is_file()
