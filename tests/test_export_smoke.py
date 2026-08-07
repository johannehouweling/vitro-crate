"""End-to-end on-disk export smoke test.

Builds crates to disk and asserts the whole written package is correct, guarding
the export pipeline that the in-memory eval (#59) predates:

* a golden-fixture crate writes ``ro-crate-metadata.json`` + ``ro-crate-preview.html``
  (#86) and round-trips clean through the on-disk validator at REQUIRED, offline (#117);
* a File referencing a real local file is copied into the payload (#128).
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate
from profiles.validator import validate_crate
from tests.fixtures.vhps_golden_crates import vhps_fixture_state

# This module writes a real crate to disk and runs the uncached, owlrl-heavy
# on-disk validator over all three passes — ~23s locally, and the 2-vCPU CI
# runner is slower still, so it sat just under the CI-wide `--timeout=30` and
# eventually tipped over it. Same headroom every other SHACL/export-heavy module
# already takes (test_pipeline_e2e, test_e2e_agent_eval, test_csvw_payload, …).
# The budget is headroom, not a licence to grow: the test is unchanged.
pytestmark = pytest.mark.timeout(120)


class TestGoldenCrateExport:
    """A golden fixture exports a complete, valid, browsable crate."""

    def test_exports_metadata_preview_and_validates_clean(self, tmp_path: Path) -> None:
        state = vhps_fixture_state("S-VHPS21")
        out = tmp_path / "crate"
        state.metadata.output_path = str(out)

        res = build_crate(state)
        assert res["success"], res["error"]
        assert res["crate_path"] == str(out)

        # The written package is browsable without tooling.
        assert (out / "ro-crate-metadata.json").is_file()
        assert (out / "ro-crate-preview.html").is_file()  # #86

        # The on-disk crate round-trips clean at REQUIRED (offline, #117).
        results = validate_crate(out)
        assert results, "validator returned no results"
        failed = [r.profile for r in results if not r.passed_required]
        assert not failed, f"REQUIRED issues in passes: {failed}"


class TestPayloadCopiedEndToEnd:
    """A referenced local file lands in the written crate's payload (#128)."""

    def test_referenced_file_copied_to_disk(self, tmp_path: Path) -> None:
        inp = tmp_path / "in"
        (inp / "data").mkdir(parents=True)
        (inp / "data" / "smoke.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        out = tmp_path / "out"

        state = CrateState()
        state.session_id = "smoke"
        state.metadata.input_path = str(inp)
        state.metadata.output_path = str(out)
        state.add_entity(
            Entity(
                entity_id="f1",
                type="File",
                fields={"name": "smoke.csv", "path": "data/smoke.csv"},
                _provenance=EntityProvenance(created_by="scanner"),
            )
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            res = build_crate(state)

        assert res["success"], res["error"]
        copied = out / "data" / "smoke.csv"
        assert copied.is_file(), "payload file was not copied into the crate"
        assert copied.read_text(encoding="utf-8") == "a,b\n1,2\n"
        assert (out / "ro-crate-preview.html").is_file()  # #86, same export path
        no_source = [str(w.message) for w in caught if "No source" in str(w.message)]
        assert not no_source, f"unexpected No-source warnings: {no_source}"
