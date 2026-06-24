"""Tests for the RO-Crate maturity report (#85)."""

from __future__ import annotations

import json
from pathlib import Path

from builder.state import CrateState, ValidationReport
from builder.tools.builder import build_crate, export_crate
from builder.writers.maturity_report import REPORT_FILENAME, build_maturity_html
from tests.fixtures.vhps_golden_crates import vhps_fixture_state


class TestBuildMaturityHtml:
    """build_maturity_html renders the four report axes (pure, no validator)."""

    def test_sections_present(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        page = build_maturity_html(state)
        assert "<html" in page.lower()
        for heading in ("Profile adherence", "FAIR", "OECD MIT", "Reproducibility readiness"):
            assert heading in page, f"missing section: {heading}"
        assert "DSM" in page  # FAIR maturity level
        assert "%" in page  # MIT coverage

    def test_conformance_suggestions_rendered(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        validation = ValidationReport(
            base_passed=False,
            isa_passed=True,
            tox_passed=True,
            required_issues=["root MUST have a name"],
            should_issues=["consider adding a license"],
        )
        page = build_maturity_html(state, validation=validation)
        assert "Must fix" in page
        assert "root MUST have a name" in page
        assert "consider adding a license" in page

    def test_unvalidated_state_notes_not_validated(self) -> None:
        # Fresh state with a default (empty) ValidationReport.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        assert "not yet validated" in page.lower()

    def test_escapes_html(self) -> None:
        state = CrateState()
        state.metadata.title = "<script>alert(1)</script>"
        page = build_maturity_html(state)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


class TestEmbeddedInCrate:
    """export_crate embeds ro-crate-maturity.html as a CreativeWork about ./."""

    def test_export_writes_maturity_report(self, tmp_path: Path) -> None:
        state = vhps_fixture_state("S-VHPS21")
        out = tmp_path / "crate"
        state.metadata.output_path = str(out)
        # Simulate the agent having validated before export — the report renders
        # this existing state.validation (no re-validate inside export).
        state.validation = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        res = build_crate(state)
        assert res["success"], res["error"]

        report = out / REPORT_FILENAME
        assert report.is_file()
        page = report.read_text(encoding="utf-8")
        assert "Profile adherence" in page
        assert "not yet validated" not in page.lower()

    def test_report_referenced_in_metadata(self, tmp_path: Path) -> None:
        state = vhps_fixture_state("S-VHPS21")
        out = tmp_path / "crate"
        state.metadata.output_path = str(out)
        build_crate(state)
        meta = json.loads((out / "ro-crate-metadata.json").read_text(encoding="utf-8"))
        entry = next(
            (e for e in meta["@graph"] if e.get("@id") == REPORT_FILENAME), None
        )
        assert entry is not None, "maturity report not referenced in metadata"
        assert "CreativeWork" in (entry.get("@type") or [])
        assert entry.get("about") == {"@id": "./"}

    def test_embed_report_false_skips(self, tmp_path: Path) -> None:
        state = vhps_fixture_state("S-VHPS21")
        out = tmp_path / "crate"
        res = export_crate(state, str(out), embed_report=False)
        assert res["success"], res["error"]
        assert not (out / REPORT_FILENAME).exists()
