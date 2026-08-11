"""The crate must not describe a file it does not contain (#530).

The SHACL validator asks this question — "is every declared Data Entity part of
the payload?" — but it can only answer it when there is a payload to look at.
Export validates the assembled *document*, where no file exists, so the check
emits nothing and silence reads as a pass: a crate whose base profile fails on
disk ships with a report headlining "Conformant".

The fix does not enumerate the check: check ids move between upstream releases
and a hardcoded list needs an edit per release. It states the invariant the
crate itself must satisfy — every local data entity is backed by a source the
write will materialise — and checks that where it is answerable, which is
everywhere a crate is assembled, for any deposit.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rocrate.model.file import File
from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, ValidationReport
from builder.tools.builder import export_crate
from builder.tools.validation import verify_payload
from tests.fixtures.vhps_golden_crates import vhps_fixture_state

pytestmark = pytest.mark.timeout(120)


class TestVerifyPayload:
    """The invariant, stated over the assembled crate rather than a check id."""

    def test_a_data_entity_with_no_source_is_reported(self) -> None:
        # Exactly what ro-crate-py does with a source-less file: it warns, writes
        # the metadata, and writes no bytes — leaving the crate describing a file
        # that is not there.
        crate = ROCrate()
        crate.add_file(source=None, dest_path="data/ghost.csv", properties={"name": "ghost"})

        issues = verify_payload(crate)

        assert [i["entity_id"] for i in issues] == ["data/ghost.csv"]
        assert issues[0]["severity"] == "required"
        assert issues[0]["profile"] == "base"
        assert "data/ghost.csv" in issues[0]["message"]

    def test_a_backed_data_entity_is_not_reported(self, tmp_path: Path) -> None:
        real = tmp_path / "real.csv"
        real.write_text("well_id,value\nA1,1\n", encoding="utf-8")
        crate = ROCrate()
        crate.add_file(source=str(real), dest_path="data/real.csv")

        assert verify_payload(crate) == []

    def test_in_memory_sources_are_not_reported(self) -> None:
        # The crate's own generated artifacts (preview, graph, maturity report)
        # carry their content as an in-memory source. They are materialised by
        # write() and must never be flagged.
        crate = ROCrate()
        crate.add(File(crate, StringIO("<html></html>"), dest_path="ro-crate-preview.html"))

        assert verify_payload(crate) == []

    def test_remote_data_entities_are_not_reported(self) -> None:
        # A data entity identified by an absolute URI lives elsewhere by design;
        # there is nothing for this crate to materialise.
        crate = ROCrate()
        crate.add_file(
            source="https://example.org/remote.csv",
            fetch_remote=False,
            validate_url=False,
        )

        assert verify_payload(crate) == []


class TestExportRefusesToClaimAPayloadItLacks:
    """The end-to-end regression: the shipped report must not say Conformant."""

    @staticmethod
    def _state_declaring_a_missing_file(tmp_path: Path) -> CrateState:
        """A crate that declares a data file whose path does not resolve.

        This is the generic shape of the defect, not one deposit's: assembly maps
        a declared file to ``source=None`` whenever its path is not an existing
        file (``_crate_mapping.py``), so any lost, renamed, or never-written
        output produces it.
        """
        state = vhps_fixture_state("S-VHPS21")
        state.metadata.output_path = str(tmp_path / "crate")
        state.add_entity(
            Entity(
                entity_id="file_vanished",
                type="File",
                fields={
                    "path": str(tmp_path / "never_written.csv"),
                    "filename": "never_written.csv",
                    "name": "never_written.csv",
                },
            )
        )
        return state

    def test_export_records_the_missing_file_as_a_required_issue(self, tmp_path: Path) -> None:
        state = self._state_declaring_a_missing_file(tmp_path)

        result = export_crate(state, str(tmp_path / "crate"))

        assert result["success"], result["error"]
        assert any("never_written.csv" in issue for issue in state.validation.required_issues), (
            state.validation.required_issues
        )

    def test_the_embedded_report_does_not_headline_conformant(self, tmp_path: Path) -> None:
        state = self._state_declaring_a_missing_file(tmp_path)

        export_crate(state, str(tmp_path / "crate"))

        page = (tmp_path / "crate" / "ro-crate-metadata-maturity.html").read_text(encoding="utf-8")
        body = page.split("</style>", 1)[-1]
        assert "Not conformant" in body
        assert '<span class="vpill good">' not in body

    def test_a_crate_whose_payload_is_whole_still_passes(self, tmp_path: Path) -> None:
        """The same fixture, whole, keeps its green verdict.

        Paired with the test above: without this, "Not conformant appears" would
        pass just as well on a fixture that was never conformant to begin with,
        and the assertion would be pinning nothing.
        """
        state = vhps_fixture_state("S-VHPS21")
        state.metadata.output_path = str(tmp_path / "crate")

        export_crate(state, str(tmp_path / "crate"))

        assert verify_payload_of(tmp_path / "crate") == []
        assert state.validation.payload_checked is True
        assert state.validation.required_issues == []
        body = (
            (tmp_path / "crate" / "ro-crate-metadata-maturity.html")
            .read_text(encoding="utf-8")
            .split("</style>", 1)[-1]
        )
        assert '<span class="vpill good">' in body
        assert "Not conformant" not in body


def verify_payload_of(crate_dir: Path) -> list[str]:
    """Every data entity in the written crate resolves on disk (test helper)."""
    import json
    import urllib.parse

    graph = json.loads((crate_dir / "ro-crate-metadata.json").read_text())["@graph"]
    missing = []
    for entity in graph:
        eid = str(entity.get("@id", ""))
        types = entity.get("@type")
        types = types if isinstance(types, list) else [types]
        if "File" not in types or eid.startswith(("#", "http://", "https://")):
            continue
        if not (crate_dir / urllib.parse.unquote(eid)).exists():
            missing.append(eid)
    return missing


class TestVerdictRecordsWhetherThePayloadWasSeen:
    """A verdict that never observed the payload must not be read as one that did."""

    def test_defaults_to_unchecked_and_round_trips(self) -> None:
        assert ValidationReport().payload_checked is False
        report = ValidationReport(payload_checked=True)
        assert ValidationReport.from_dict(report.to_dict()).payload_checked is True
        # A checkpoint written before the field existed still loads.
        legacy = report.to_dict()
        del legacy["payload_checked"]
        assert ValidationReport.from_dict(legacy).payload_checked is False

    def test_a_clean_metadata_only_verdict_says_the_files_were_not_checked(self) -> None:
        from builder.writers.maturity_report import build_maturity_html

        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            assessed_tiers={"required", "recommended", "optional"},
        )
        page = build_maturity_html(state, validation=val)
        assert "covers the metadata document only" in page

        val.payload_checked = True
        assert "covers the metadata document only" not in build_maturity_html(state, validation=val)

    def test_the_caveat_survives_a_crate_that_has_advisory_findings(self) -> None:
        """The green pill over-claims hardest exactly here.

        A verdict clean at REQUIRED but carrying advisory findings still heads
        the report with "Conformant" — and renders no empty-state note. Hanging
        the caveat off that note would drop it in the one case where it matters
        most, so it is rendered independently of whether findings exist.
        """
        from builder.writers.maturity_report import build_maturity_html

        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            should_issues=["[base] ./: add a license"],
            assessed_tiers={"required", "recommended", "optional"},
        )
        page = build_maturity_html(state, validation=val)
        assert '<span class="vpill good">' in page, "precondition: the pill still reads Conformant"
        assert "covers the metadata document only" in page

    def test_the_on_disk_validator_records_that_it_saw_the_payload(self, tmp_path: Path) -> None:
        # validate() runs over a directory, so the payload checks the in-memory
        # gate cannot run did run — its verdict must not carry the caveat.
        from builder.tools.validation import validate

        crate_dir = tmp_path / "crate"
        crate_dir.mkdir()
        (crate_dir / "ro-crate-metadata.json").write_text(
            '{"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": []}'
        )

        assert validate(CrateState(), str(crate_dir)).payload_checked is True

    def test_the_in_memory_gate_leaves_it_unchecked(self) -> None:
        # build_and_validate cannot see a payload, so it must not claim to have.
        from builder.tools.validation import apply_validation_result

        state = vhps_fixture_state("S-VHPS21")
        apply_validation_result(
            state,
            "build_and_validate",
            {"ok": True, "conformance": {"base": True, "isa": True, "tox": True}, "issues": []},
            severity="required",
        )
        assert state.validation.payload_checked is False
