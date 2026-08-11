"""Tests for the RO-Crate maturity report (#85)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.state import CrateState, ValidationReport
from builder.tools.builder import build_crate, export_crate
from builder.writers.maturity_report import REPORT_FILENAME, build_maturity_html
from tests.fixtures.vhps_golden_crates import vhps_fixture_state

# Every test here exports a crate, and each export now runs the uncached,
# owlrl-heavy validator over all three profiles at the full severity gate (#446)
# — ~10s per export locally, and the 2-vCPU CI runner is ~2-3x slower, which puts
# the whole module against the CI-wide `--timeout=30`. Same headroom, for the
# same reason, that the other export-heavy modules already take
# (test_export_smoke, test_readers, test_path_traversal, test_html_xss).
# Headroom, not a licence to grow: no test in this module is changed.
pytestmark = pytest.mark.timeout(120)

def _body(page: str) -> str:
    """The rendered markup without the inlined stylesheet.

    The report embeds its whole CSS in a ``<style>`` block, and that block names
    every tab id it styles — so an "``id=…`` is absent" assertion against the raw
    page would match the stylesheet and pass for the wrong reason.
    """
    return page.split("</style>", 1)[-1]


class TestReportFilename:
    """The report filename shares the crate's ``ro-crate-metadata`` stem."""

    def test_report_filename_is_metadata_stemmed(self) -> None:
        # Consistency with the crate's main file (ro-crate-metadata.json); still
        # an .html document because build_maturity_html renders HTML.
        assert REPORT_FILENAME == "ro-crate-metadata-maturity.html"


class TestBuildMaturityHtml:
    """build_maturity_html renders the four report axes (pure, no validator)."""

    def test_sections_present(self) -> None:
        """Renders the four report axes AND the COMPUTED FAIR/MIT scores.

        Asserting the static labels alone (``"DSM"`` / ``"%"``) passed even if the
        report rendered a label with no value behind it; here a real DSM level (0-5)
        and a MIT coverage percentage NUMBER must be present, so a regression that
        stops rendering the computed score fails.
        """
        import re

        state = vhps_fixture_state("S-VHPS21")
        page = build_maturity_html(state)
        assert "<html" in page.lower()
        for heading in ("Profile adherence", "FAIR", "OECD MIT", "Reproducibility readiness"):
            assert heading in page, f"missing section: {heading}"
        # Computed scores, not just the static labels:
        assert re.search(r"DSM level [0-5] of 5", page), "no computed DSM level rendered"
        assert re.search(r"\d+(?:\.\d+)?\s*%", page), "no computed MIT percentage rendered"

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

    def test_mit_guidance_document_breakdown_rendered(self, tmp_path: Path) -> None:
        """#491: under the module rows, the MIT section breaks coverage out per
        guidance document. Each row's rendered numerator AND denominator must
        equal the scorer's own bucket — a label-plus-denominator assertion
        survived a mutant that hardcoded every numerator to 0 — and the
        aggregate headline must stay the module-bucket sums, since summing the
        overlapping per-document buckets is exactly the double-count the
        overlap note warns about. Scored on the graph path so the pinned
        numerators are non-vacuously non-zero."""
        import re

        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate
        from builder.tools.mit_assessment import (
            MIT_STANDARD_LABELS,
            assess_mit_coverage,
        )
        from profiles.context import ISA_TOX_CONTEXT

        state = vhps_fixture_state("S-VHPS21")
        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(state, crate, tmp_path, materialize_payload=False)
        graph = crate.metadata.generate()["@graph"]
        mit = assess_mit_coverage(state, graph=graph)

        page = build_maturity_html(state, graph=graph)
        assert set(mit.standard_scores) == set(MIT_STANDARD_LABELS)
        assert any(b["completed"] > 0 for b in mit.standard_scores.values())
        for key, bucket in mit.standard_scores.items():
            label = MIT_STANDARD_LABELS[key]
            m = re.search(
                re.escape(label) + r'</div>.*?(\d+)<span class="den">/(\d+)</span>',
                page,
                re.S,
            )
            assert m, f"no per-document row for {label}"
            assert (int(m.group(1)), int(m.group(2))) == (
                bucket["completed"],
                bucket["total"],
            ), label
        # The aggregate stays the headline, summed over the MODULE buckets —
        # not inflated by the overlapping per-document ones.
        head = re.search(
            r'OECD MIT coverage</h2><span class="sec-meta"><b>(\d+)/(\d+)</b> fields',
            page,
        )
        assert head, "aggregate headline fraction not found"
        assert int(head.group(1)) == sum(
            sc["completed"] for sc in mit.module_scores.values()
        )
        assert int(head.group(2)) == sum(
            sc["total"] for sc in mit.module_scores.values()
        )
        # Documents overlap — one parameter can serve several — so the note
        # that rows don't sum is part of the contract, not decoration.
        assert "do not sum" in page


class TestEmbeddedInCrate:
    """export_crate embeds ro-crate-metadata-maturity.html as a CreativeWork about ./."""

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
        entry = next((e for e in meta["@graph"] if e.get("@id") == REPORT_FILENAME), None)
        assert entry is not None, "maturity report not referenced in metadata"
        assert "CreativeWork" in (entry.get("@type") or [])
        assert entry.get("about") == {"@id": "./"}

    def test_embed_report_false_skips(self, tmp_path: Path) -> None:
        state = vhps_fixture_state("S-VHPS21")
        out = tmp_path / "crate"
        res = export_crate(state, str(out), embed_report=False)
        assert res["success"], res["error"]
        assert not (out / REPORT_FILENAME).exists()


class TestStaleValidation:
    """A verdict recorded against a DIFFERENT crate is never reported as a pass.

    The agent keeps editing after validating, so ``state.validation`` can outrun
    the crate. Rendering the old verdict would ship a green "Conformant" inside
    the exported crate for a state nobody checked — strictly worse than admitting
    the gap, because it looks verified.
    """

    def _validated(self) -> CrateState:
        state = vhps_fixture_state("S-VHPS21")
        state.validation = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            input_fingerprint=state.validation_fingerprint(),
        )
        return state

    def _verdict(self, page: str) -> str:
        import re

        m = re.search(r'<span class="vpill (\w+)"><span class="glyph"></span>([^<]*)', page)
        assert m, "no verdict pill rendered"
        return m.group(2)

    def test_fresh_verdict_still_reports_conformant(self) -> None:
        assert self._verdict(build_maturity_html(self._validated())) == "Conformant"

    def test_edited_after_validating_is_reported_out_of_date(self) -> None:
        state = self._validated()
        state.metadata.title = "Edited after validating"
        assert self._verdict(build_maturity_html(state)) == "Validation out of date"

    def test_stale_report_makes_no_pass_claim_anywhere(self) -> None:
        state = self._validated()
        state.metadata.title = "Edited after validating"
        page = build_maturity_html(state)
        assert "Conformant" not in page
        # The tier summary asserts a pass as loudly as a green tick.
        assert "3 / 3 profiles" not in page
        assert "out of date" in page
        assert "Re-run validation" in page

    def test_unstamped_verdict_is_trusted_not_flagged(self) -> None:
        # A checkpoint written before the stamp existed must not be downgraded.
        state = vhps_fixture_state("S-VHPS21")
        state.validation = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        assert self._verdict(build_maturity_html(state)) == "Conformant"


class TestExportCoupledToValidation:
    """``export_crate`` validates before writing unless the verdict is current.

    The export embeds a maturity report whose headline comes from
    ``state.validation``; without this a crate edited since its last validation
    ships a report describing a state nobody checked.
    """

    def _stub(self, monkeypatch, calls: list) -> None:
        import builder.tools.validation as validation

        def _fake(state, severity="required", profile="all"):
            calls.append(severity)
            return {
                "ok": True,
                "conformance": {"base": True, "isa": True, "tox": True},
                "issues": [],
            }

        monkeypatch.setattr(validation, "build_and_validate", _fake)

    def test_validates_when_never_validated(self, monkeypatch, tmp_path: Path) -> None:
        calls: list = []
        self._stub(monkeypatch, calls)
        res = export_crate(vhps_fixture_state("S-VHPS21"), str(tmp_path / "c"))
        assert res["success"]
        assert res["validation"]["ran"] is True
        assert res["validation"]["reason"] == "never-validated"
        # The export gates on EVERY tier: the report it embeds describes
        # RECOMMENDED and OPTIONAL too, so a REQUIRED-only sweep would leave it
        # claiming tiers nobody checked.
        assert calls == ["optional"]

    def test_skips_when_the_verdict_is_already_current(self, monkeypatch, tmp_path: Path) -> None:
        calls: list = []
        self._stub(monkeypatch, calls)
        state = vhps_fixture_state("S-VHPS21")
        export_crate(state, str(tmp_path / "c1"))
        res = export_crate(state, str(tmp_path / "c2"))
        assert res["validation"]["ran"] is False
        assert res["validation"]["reason"] == "fresh"
        assert res["validation"]["ok"] is None
        assert res["validation"]["error"] is None
        assert calls == ["optional"], "re-validated an unchanged crate"

    def test_revalidates_after_the_crate_changes(self, monkeypatch, tmp_path: Path) -> None:
        calls: list = []
        self._stub(monkeypatch, calls)
        state = vhps_fixture_state("S-VHPS21")
        export_crate(state, str(tmp_path / "c1"))
        state.metadata.title = "Edited after export"
        res = export_crate(state, str(tmp_path / "c2"))
        assert res["validation"]["reason"] == "stale"
        assert calls == ["optional", "optional"]

    def test_embedded_report_reflects_the_fresh_verdict(self, monkeypatch, tmp_path: Path) -> None:
        calls: list = []
        self._stub(monkeypatch, calls)
        out = tmp_path / "c"
        export_crate(vhps_fixture_state("S-VHPS21"), str(out))
        page = (out / REPORT_FILENAME).read_text(encoding="utf-8")
        assert "not yet validated" not in page.lower()
        assert "Validation out of date" not in page

    def test_failing_validation_still_writes_the_crate(self, monkeypatch, tmp_path: Path) -> None:
        # A hard gate would throw away the agent loop's end-of-session salvage;
        # the crate is written and the report states the real verdict.
        import builder.tools.validation as validation

        def _failing(state, severity="required", profile="all"):
            return {
                "ok": False,
                "conformance": {"base": False, "isa": False, "tox": False},
                "issues": [
                    {
                        "entity_id": "./",
                        "property": "name",
                        "message": "root MUST have a name",
                        "severity": "required",
                        "profile": "base",
                    }
                ],
            }

        monkeypatch.setattr(validation, "build_and_validate", _failing)
        out = tmp_path / "c"
        res = export_crate(vhps_fixture_state("S-VHPS21"), str(out))
        assert res["success"] is True
        assert (out / "ro-crate-metadata.json").is_file()
        page = (out / REPORT_FILENAME).read_text(encoding="utf-8")
        assert "Not conformant" in page
        assert "root MUST have a name" in page

    def test_validator_failure_does_not_fail_the_export(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import builder.tools.validation as validation

        def _boom(state, severity="required", profile="all"):
            raise RuntimeError("shapes graph unavailable")

        monkeypatch.setattr(validation, "build_and_validate", _boom)
        out = tmp_path / "c"
        res = export_crate(vhps_fixture_state("S-VHPS21"), str(out))
        assert res["success"] is True
        assert (out / "ro-crate-metadata.json").is_file()
        assert "shapes graph unavailable" in res["validation"]["error"]

    def test_validate_false_skips_entirely(self, monkeypatch, tmp_path: Path) -> None:
        calls: list = []
        self._stub(monkeypatch, calls)
        res = export_crate(
            vhps_fixture_state("S-VHPS21"), str(tmp_path / "c"), validate=False
        )
        assert res["success"] is True
        assert "validation" not in res
        assert calls == []


class TestProvenanceSection:
    """When a crate ``@graph`` is supplied, the report folds in a Provenance &
    graph section: the derivation-chain SVG plus a graph-topology strip (#85)."""

    def _chain_graph(self) -> dict:
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#d"}]},
                {"@id": "#s", "@type": "Sample", "name": "Input sample"},
                {
                    "@id": "#p",
                    "@type": "LabProcess",
                    "additionalType": "Exposure",
                    "object": {"@id": "#s"},
                    "result": {"@id": "#d"},
                },
                {"@id": "#d", "@type": "File", "name": "result.csv"},
            ]
        }

    def test_graph_renders_provenance_and_topology(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        page = build_maturity_html(state, graph=self._chain_graph())
        assert "Provenance" in page
        assert 'class="prov"' in page  # the inline derivation-chain SVG
        assert "result.csv" in page
        assert "Graph topology" in page  # the relocated topology strip
        assert "entities" in page

    def test_no_graph_omits_provenance_section(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        assert "Graph topology" not in page
        assert 'class="prov"' not in page

    def test_graph_without_chain_shows_topology_note(self) -> None:
        # Entities but no LabProcess I/O → topology strip, but no chain SVG.
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset"},
                {"@id": "#f", "@type": "File", "name": "orphan.csv"},
            ]
        }
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "Graph topology" in page
        assert 'class="prov"' not in page
        assert "no derivation chain" in page.lower()

    def test_graph_gives_nonzero_mit_coverage(self, tmp_path: Path) -> None:
        # MIT coverage is scored against the assembled @graph (crate_slot vocab
        # describes the serialized crate, not CrateState), so a real crate reports
        # non-zero coverage in the report — not the old 0% (#311).
        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate
        from profiles.context import ISA_TOX_CONTEXT

        state = vhps_fixture_state("S-VHPS21")
        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(state, crate, tmp_path, materialize_payload=False)
        graph = crate.metadata.generate()["@graph"]

        def _mit_pct(page: str) -> int:
            import re

            m = re.search(
                r'OECD MIT coverage.*?<b>(\d+)</b><span class="den">%', page, re.S
            )
            assert m, "MIT KPI tile not found"
            return int(m.group(1))

        assert _mit_pct(build_maturity_html(state, graph=graph)) > 0
        # Without the graph the report falls back (cheap) and this fixture scores 0.
        assert _mit_pct(build_maturity_html(state)) == 0

    def test_export_embeds_provenance_from_crate_graph(self, tmp_path: Path) -> None:
        # End-to-end: the embedded report is built with the crate's real @graph,
        # so the topology strip travels with the written crate.
        state = vhps_fixture_state("S-VHPS21")
        out = tmp_path / "crate"
        state.metadata.output_path = str(out)
        build_crate(state)
        page = (out / REPORT_FILENAME).read_text(encoding="utf-8")
        assert "Graph topology" in page


class TestActionableTopology:
    """The topology strip's orphan/dangling counts are made *actionable* (#310):
    a bounded ``<details>`` lists which entities are orphaned and which references
    dangle, so a reader can fix them — not just a bare count. Read straight off the
    existing ``build_crate_graph`` node model (pure/cheap, no re-validation)."""

    def _messy_graph(self) -> dict:
        # One orphan (#orphan, unreachable from root) and one dangling ref
        # (#ghost, referenced by #p2 but has no entity). The chain #in→#p→#out is
        # reachable and clean.
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "hasPart": [{"@id": "#p"}, {"@id": "#out"}, {"@id": "#p2"}],
                },
                {"@id": "#in", "@type": "Sample", "name": "Input sample"},
                {
                    "@id": "#p",
                    "@type": "LabProcess",
                    "additionalType": "Exposure",
                    "object": {"@id": "#in"},
                    "result": {"@id": "#out"},
                },
                {"@id": "#out", "@type": "File", "name": "result.csv"},
                {
                    "@id": "#p2",
                    "@type": "LabProcess",
                    "additionalType": "DataAnalysis",
                    "object": {"@id": "#out"},
                    "result": {"@id": "#ghost"},
                },
                {"@id": "#orphan", "@type": "File", "name": "loose.csv"},
            ]
        }

    def test_lists_orphan_entities(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._messy_graph())
        # The count still renders in the strip…
        assert "1 orphan" in page
        # …and now the disclosure names the orphan (id + label + type).
        assert 'class="disc topo-detail"' in page
        assert "#orphan" in page
        assert "loose.csv" in page

    def test_lists_dangling_reference_targets(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._messy_graph())
        assert "1 dangling ref" in page
        assert "#ghost" in page

    def test_no_disclosure_when_topology_clean(self) -> None:
        # A clean crate (no orphans, no dangling refs) renders the strip but no
        # actionable disclosure — nothing empty to open.
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#d"}]},
                {"@id": "#d", "@type": "File", "name": "result.csv"},
            ]
        }
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "Graph topology" in page
        assert 'class="disc topo-detail"' not in page

    def test_orphan_list_is_bounded_with_more_marker(self) -> None:
        # 12 orphaned files → only the first 10 listed, then "+2 more" (nothing
        # silently dropped).
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#kept"}]},
                {"@id": "#kept", "@type": "File", "name": "kept.csv"},
            ]
        }
        for i in range(12):
            graph["@graph"].append(
                {"@id": f"#loose{i}", "@type": "File", "name": f"loose{i}.csv"}
            )
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "12 orphans" in page
        assert "+2 more" in page
        # First 10 are listed; the 11th/12th are folded into the "+N more".
        assert "#loose0" in page
        assert "#loose9" in page
        assert "#loose11" not in page


class TestChemicalsSection:
    """The Chemicals section (#85): how each compound reaches the experiment and
    how completely it is identified.

    ISA forbids a MolecularEntity as a LabProcess ``object``, so a compound is
    only ever connected *through* the Exposure's condition table. A crate can
    therefore pass every profile while every compound sits orphaned — described
    in full, but unreachable from the experiment that used it. The section must
    make that state visible instead of scoring the compounds on description alone.
    """

    def _graph(self, *, wire: bool = True) -> dict:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#table"}]},
                {"@id": "#cells", "@type": "Sample", "name": "Cultured cells"},
                {
                    "@id": "#exposure",
                    "@type": "LabProcess",
                    "additionalType": "Exposure",
                    "name": "Exposure step",
                    "object": {"@id": "#cells"},
                    "result": {"@id": "#table"},
                },
                {
                    "@id": "#table",
                    "@type": ["File", "csvw:Table"],
                    "name": "Condition table",
                    **({"about": [{"@id": "#compound"}]} if wire else {}),
                },
                {
                    "@id": "#compound",
                    "@type": "MolecularEntity",
                    "name": "Aflatoxin B1",
                    "inchikey": "OQIQSTLJSLGHID-WNWIJWBNSA-N",
                    "smiles": "CO",
                    "formula": "C17H12O6",
                    "mass": "312.3",
                    "identifier": [{"@id": "#cas"}],
                },
                {"@id": "#cas", "@type": "PropertyValue", "name": "CAS", "value": "1162-65-8"},
            ]
        }
        return graph

    def _page(self, **kw: bool) -> str:
        return build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph(**kw))

    def test_section_renders_diagram_and_matrix(self) -> None:
        page = self._page()
        assert '<div class="panel" id="p-chem">' in page
        assert 'class="prov view"' in page  # the inline route diagram
        assert 'class="chem-tbl"' in page  # the identification matrix
        assert "Aflatoxin B1" in page
        # Matrix columns name the identification fields.
        for column in ("CAS", "CID", "DTXSID", "InChIKey", "SMILES", "Formula", "Mass"):
            assert f">{column}</th>" in page, f"missing coverage column: {column}"

    def test_wired_compound_reports_a_clean_route(self) -> None:
        page = self._page(wire=True)
        assert "Every compound is reachable from the process that used it." in page
        assert "cannot be reached from any process" not in page

    def test_unwired_compound_is_called_out_with_the_fix(self) -> None:
        page = self._page(wire=False)
        assert "1 of 1 compounds cannot be reached from any process." in page
        # The callout names the actual remedy, not just the defect.
        assert "condition table" in page
        assert "<code>about</code>" in page
        assert 'class="chem-flag"' in page

    def test_identification_is_scored_separately_from_wiring(self) -> None:
        # An unwired compound can still be perfectly identified; conflating the
        # two would hide which of the two problems the crate actually has.
        page = self._page(wire=False)
        assert "cannot be reached from any process" in page
        # CAS + InChIKey + SMILES + Formula + Mass of 7 fields — the compound is
        # well described and still unreachable; both must be reported.
        assert "<b>71%</b> identified" in page

    def test_kpi_tile_reports_wired_over_total(self) -> None:
        import re

        page = self._page(wire=False)
        assert re.search(
            r'<span class="eyebrow">Chemicals</span>.*?<b>0</b><span class="den">/ 1</span>',
            page,
            re.S,
        ), "chemicals KPI tile missing or not reporting 0 / 1 wired"

    def test_crate_without_compounds_omits_the_section(self) -> None:
        # "Not applicable" must not render as an empty panel scoring zero.
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#f"}]},
                {"@id": "#f", "@type": "File", "name": "result.csv"},
            ]
        }
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph))
        assert 'id="p-chem"' not in body
        assert 'for="mv-chem"' not in body
        assert '<span class="eyebrow">Chemicals</span>' not in body

    def test_no_graph_omits_the_section(self) -> None:
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21")))
        assert 'id="p-chem"' not in body
        assert "Graph views" not in body

    def test_matrix_lists_every_compound(self) -> None:
        # Uncapped, matching the diagram: this is a metadata-checking view, and a
        # truncated tail hides exactly the rows worth acting on.
        graph = {"@graph": [{"@id": "./", "@type": "Dataset"}]}
        for i in range(15):
            graph["@graph"].append(
                {"@id": f"#c{i}", "@type": "MolecularEntity", "name": f"Compound {i:02d}"}
            )
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        for i in range(15):
            assert f"Compound {i:02d}" in page, f"compound {i} missing from the matrix"
        assert "more compounds" not in page

    def test_escapes_compound_names(self) -> None:
        graph = {
            "@graph": [
                {
                    "@id": "#c",
                    "@type": "MolecularEntity",
                    "name": "<script>alert(1)</script>",
                }
            ]
        }
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_section_stays_self_contained(self) -> None:
        # The report is offline/no-script; the chemicals diagram must not break
        # that (it is finished SVG, like the derivation chain).
        page = self._page()
        panel = page.split('<div class="panel" id="p-chem">', 1)[1].split("</div>", 1)[0]
        assert "<script" not in panel.lower()
        assert "src=" not in panel and "@import" not in panel


class TestGraphViewTabs:
    """The three diagrams share one tabbed section (#85).

    The report is a self-contained offline artifact embedded in the crate, so the
    tabs must work with no script: radio inputs plus ``:checked ~`` sibling CSS.
    That constrains the markup — the inputs must PRECEDE both the tab bar and the
    panels as siblings, or the sibling combinator never matches and every panel
    stays hidden.
    """

    def _graph(self) -> dict:
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "Crate",
                    "hasPart": [{"@id": "#table"}],
                    "author": [{"@id": "https://orcid.org/0000-0002-1825-0097"}],
                },
                {"@id": "#cells", "@type": "Sample", "name": "Cultured cells"},
                {
                    "@id": "#line",
                    "@type": "Sample",
                    "additionalType": "CellLine",
                    "name": "CHO-K1",
                    "identifier": "CVCL_0214",
                },
                {
                    "@id": "#culture",
                    "@type": "LabProcess",
                    "additionalType": "CellCulture",
                    "name": "Cell culture",
                    "input": {"@id": "#line"},
                    "output": {"@id": "#cells"},
                },
                {
                    "@id": "#exposure",
                    "@type": "LabProcess",
                    "additionalType": "Exposure",
                    "name": "Exposure step",
                    "object": {"@id": "#cells"},
                    "result": {"@id": "#table"},
                },
                {
                    "@id": "#table",
                    "@type": ["File", "csvw:Table"],
                    "name": "Condition table",
                    "about": [{"@id": "#compound"}],
                },
                {"@id": "#compound", "@type": "MolecularEntity", "name": "Aflatoxin B1"},
                {
                    "@id": "https://orcid.org/0000-0002-1825-0097",
                    "@type": "Person",
                    "name": "Josiah Carberry",
                    "affiliation": {"@id": "https://ror.org/05gq02987"},
                },
                {
                    "@id": "https://ror.org/05gq02987",
                    "@type": "Organization",
                    "name": "Brown University",
                },
            ]
        }

    def test_all_three_views_are_tabbed(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert "<h2>Graph views</h2>" in page
        for label in ("All entities", "ISA structure", "Provenance", "Chemicals",
                      "Cell lines", "People &amp; orgs"):
            assert f'<span class="tb-n">{label}</span>' in page, f"missing tab: {label}"
        for pid in ("p-all", "p-isa", "p-prov", "p-chem", "p-cell", "p-people"):
            assert f'<div class="panel" id="{pid}">' in page, f"missing panel: {pid}"

    def test_inputs_precede_the_tabbar_and_panels(self) -> None:
        # The CSS-only mechanism is `input:checked ~ .panel`; a panel emitted
        # before its radio can never be revealed.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        last_input = page.rindex('<input class="tab-in"')
        assert last_input < page.index('<div class="tabbar">')
        assert last_input < page.index('<div class="panel"')

    def test_exactly_one_tab_starts_selected(self) -> None:
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph()))
        assert body.count('name="mat-view"') == 6
        assert body.count(" checked>") == 1
        # ISA is first: the structural backbone every other view hangs off.
        assert 'id="mv-all" checked>' in body

    def test_tabs_carry_no_script(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert "<script" not in page.lower()
        assert "onclick" not in page.lower()

    def test_absent_views_drop_their_tab_and_first_survivor_is_selected(self) -> None:
        # No compounds, no cell lines and nobody credited. The root Dataset is
        # still an Investigation, so ISA survives alongside Provenance — and the
        # first surviving tab must be the selected one, never a dead tab bar.
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#d"}]},
                {"@id": "#s", "@type": "Sample", "name": "Input"},
                {
                    "@id": "#p",
                    "@type": "LabProcess",
                    "additionalType": "Exposure",
                    "object": {"@id": "#s"},
                    "result": {"@id": "#d"},
                },
                {"@id": "#d", "@type": "File", "name": "result.csv"},
            ]
        }
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph))
        for absent in ("mv-chem", "p-chem", "mv-cell", "p-cell", "mv-people", "p-people"):
            assert f'"{absent}"' not in body, f"{absent} should have been dropped"
        assert 'id="mv-all" checked>' in body
        assert body.count(" checked>") == 1

    def test_every_element_id_in_the_page_is_unique(self) -> None:
        # Several SVGs now share one document. `url(#…)` resolves to the FIRST
        # matching id in the document, and the panels holding them are
        # display:none until selected — so a duplicated marker id points one
        # diagram's arrowheads at a marker inside a hidden subtree.
        import re

        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph()))
        ids = re.findall(r' id="([^"]+)"', body)
        assert sorted(ids) == sorted(set(ids)), "duplicate element id in the report"

    def test_each_diagram_references_only_its_own_marker(self) -> None:
        import re

        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph()))
        for svg in re.findall(r"<svg .*?</svg>", body, re.S):
            defined = set(re.findall(r'<marker id="([^"]+)"', svg))
            used = set(re.findall(r"url\(#([^)]+)\)", svg))
            assert used <= defined, f"marker referenced across SVGs: {used - defined}"

    def test_routed_views_are_not_stretched_to_the_chain_width(self) -> None:
        # `.mat svg.prov` forces width:100%/min-width:44rem for the wide, fixed
        # derivation chain. The routed views size themselves to their content
        # (~210-540 units); inheriting that floor upscales a small diagram
        # several times over, so they must carry their own sizing rule.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        css = page.split("<style>", 1)[1].split("</style>", 1)[0]
        assert ".mat svg.prov.view {" in css, "routed views have no sizing rule"
        assert "min-width:0" in css.split(".mat svg.prov.view {", 1)[1].split("}", 1)[0]
        # …and the rule must actually match the class the renderer emits.
        assert 'class="prov view"' in _body(page)

    def test_print_styles_expand_every_panel(self) -> None:
        # Tabs are a screen affordance; a printed report must not silently lose
        # two of the three views.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert ".mat .panel{display:block !important;" in page.replace("\n", "")
        assert ".mat .panel > .panel-h{display:block;" in page.replace("\n", "")

    def test_topology_strip_stays_below_the_tabs(self) -> None:
        # The strip describes the whole graph, not one view — it must not be
        # trapped inside a panel that a reader has to select to see.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert page.index('<div class="panel" id="p-people">') < page.index("Graph topology")


class TestCellLinesPanel:
    """The Cell lines view (#85): the biological test system, and whether it is
    pinned down.

    A cell line fails the same two ways a compound does — unreachable when the
    ``CellCulture`` consumes a freshly minted generic ``Sample`` instead of the
    declared line, and unidentified when it carries a name but no Cellosaurus
    RRID ("CHO-K1" names a family of divergent stocks; CVCL_0214 names one).
    """

    def _graph(self, *, wire: bool = True, rrid: bool = True) -> dict:
        line: dict = {
            "@id": "#cho",
            "@type": "Sample",
            "additionalType": "CellLine",
            "name": "CHO-K1",
            "sampleType": {"@id": "#term"},
        }
        if rrid:
            line["identifier"] = "CVCL_0214"
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#cultured"}]},
                {"@id": "#generic", "@type": "Sample", "name": "Input sample"},
                {
                    "@id": "#culture",
                    "@type": "LabProcess",
                    "additionalType": "CellCulture",
                    "name": "CHO-K1 culture",
                    "input": {"@id": "#cho" if wire else "#generic"},
                    "output": {"@id": "#cultured"},
                },
                {"@id": "#cultured", "@type": "Sample", "name": "Cultured cells"},
                line,
                {"@id": "#term", "@type": "DefinedTerm", "name": "cell line"},
            ]
        }

    def _page(self, **kw: bool) -> str:
        return build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph(**kw))

    def test_renders_diagram_and_matrix(self) -> None:
        page = self._page()
        assert '<div class="panel" id="p-cell">' in page
        assert '<span class="tb-n">Cell lines</span>' in page
        assert "CHO-K1" in page
        assert "CVCL_0214" in page
        for column in ("RRID", "Type", "Organ", "Tissue", "Passage"):
            assert f">{column}</th>" in page, f"missing cell-line column: {column}"

    def test_unconsumed_line_is_called_out_with_the_fix(self) -> None:
        page = self._page(wire=False)
        assert "1 of 1 cell lines are not consumed by any process." in page
        assert "<code>CellCulture</code>" in page
        assert "<code>input</code>" in page

    def test_consumed_line_reports_a_clean_route(self) -> None:
        page = self._page(wire=True, rrid=True)
        assert "not consumed by any process" not in page

    def test_missing_rrid_is_called_out_separately_from_wiring(self) -> None:
        # Correctly consumed but unidentified: the two defects are independent
        # and collapsing them would hide whichever the crate actually has.
        page = self._page(wire=True, rrid=False)
        assert "not consumed by any process" not in page
        assert "1 of 1 cell lines carry no Cellosaurus RRID." in page

    def test_crate_without_cell_lines_omits_the_view(self) -> None:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#f"}]},
                {"@id": "#f", "@type": "File", "name": "result.csv"},
            ]
        }
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph))
        assert 'id="p-cell"' not in body
        assert 'for="mv-cell"' not in body

    def test_escapes_cell_line_names(self) -> None:
        graph = self._graph()
        graph["@graph"][5]["name"] = "<script>alert(1)</script>"
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


class TestPeoplePanel:
    """The People & organisations view (#85): who the crate credits, how resolvably.

    Attribution passes every profile with a bare ``name`` while crediting nobody a
    registry can resolve, and the classic defect — one institution minted twice,
    once ROR-backed and once locally — is invisible in a list and obvious in a
    graph where one copy has edges and the other has none.
    """

    def _graph(self, *, duplicate_org: bool = True) -> dict:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "Crate",
                    "author": [
                        {"@id": "https://orcid.org/0000-0002-1825-0097"},
                        {"@id": "#Person_no_orcid"},
                    ],
                },
                {
                    "@id": "https://orcid.org/0000-0002-1825-0097",
                    "@type": "Person",
                    "name": "Josiah Carberry",
                    "affiliation": {"@id": "https://ror.org/05gq02987"},
                },
                # Credited, but neither ORCID-backed nor affiliated.
                {"@id": "#Person_no_orcid", "@type": "Person", "name": "Jane Doe"},
                {
                    "@id": "https://ror.org/05gq02987",
                    "@type": "Organization",
                    "name": "Brown University",
                },
            ]
        }
        if duplicate_org:
            # The same institution, minted locally and referenced by nobody.
            graph["@graph"].append(
                {"@id": "#Organization_brown", "@type": "Organization", "name": "Brown Univ."}
            )
        return graph

    def _page(self, **kw: bool) -> str:
        return build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph(**kw))

    def test_renders_diagram_and_matrix(self) -> None:
        page = self._page()
        assert '<div class="panel" id="p-people">' in page
        assert "Josiah Carberry" in page
        assert "Brown University" in page
        for column in ("PID", "Name", "Affiliation", "Linked"):
            assert f">{column}</th>" in page, f"missing attribution column: {column}"

    def test_flags_the_unattached_duplicate_institution(self) -> None:
        page = self._page(duplicate_org=True)
        assert "1 of 4 agents are referenced by nothing in the crate" in page
        assert "Brown Univ." in page
        assert "duplicate" in page  # the callout names the likely cause
        # The unattached duplicate is flagged as a defect; the correctly
        # affiliation-linked institution gets only the muted route chip.
        assert ">unattached</span>" in page
        assert 'class="chem-flag muted"' in page
        assert 'class="chem-flag"' in page

    def test_flags_agents_without_a_persistent_identifier(self) -> None:
        page = self._page(duplicate_org=False)
        # Jane Doe has no ORCID; Brown University has a ROR; Carberry an ORCID.
        assert "1 of 3 agents carry no persistent identifier" in page
        assert "ORCID" in page and "ROR" in page

    def test_clean_attribution_reports_no_defect(self) -> None:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "Crate",
                    "author": [{"@id": "https://orcid.org/0000-0002-1825-0097"}],
                },
                {
                    "@id": "https://orcid.org/0000-0002-1825-0097",
                    "@type": "Person",
                    "name": "Josiah Carberry",
                    "affiliation": {"@id": "https://ror.org/05gq02987"},
                },
                {
                    "@id": "https://ror.org/05gq02987",
                    "@type": "Organization",
                    "name": "Brown University",
                },
            ]
        }
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "Every agent is credited and identifier-backed." in page
        assert "referenced by nothing" not in page

    def test_organisation_affiliation_column_is_not_a_miss(self) -> None:
        # An Organization has no affiliation of its own; scoring that as a miss
        # would penalise every crate for a field that cannot apply.
        from builder.writers.provenance_dag import build_people_inventory

        inv = build_people_inventory(self._graph(duplicate_org=False))
        org = next(a for a in inv["agents"] if a["kind"] == "org")
        assert org["fields"]["Affiliation"] is None
        assert org["total"] == 3  # PID + Name + Credited — Affiliation excluded

    def test_crate_without_agents_omits_the_view(self) -> None:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#f"}]},
                {"@id": "#f", "@type": "File", "name": "result.csv"},
            ]
        }
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert 'id="p-people"' not in page

    def test_escapes_agent_names(self) -> None:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "author": [{"@id": "#p"}]},
                {"@id": "#p", "@type": "Person", "name": "<script>alert(1)</script>"},
            ]
        }
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


class TestSeverityTiers:
    """Profile adherence reported across Required / Recommended / Optional (#306).

    The report must distinguish a tier that was assessed-and-clean from one that
    was never evaluated. The fast in-loop path (``build_and_validate``) gates at
    REQUIRED severity and never populates ``should_issues`` / ``may_issues``, so an
    empty list at those tiers means "not assessed", NOT "0 issues". Rendering an
    unevaluated tier as a green zero would be a false pass.
    """

    def _passed(self, **overrides: Any) -> ValidationReport:
        base: dict[str, Any] = {"base_passed": True, "isa_passed": True, "tox_passed": True}
        base.update(overrides)
        return ValidationReport(**base)

    def test_reports_all_three_severity_tiers(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        page = build_maturity_html(state, validation=self._passed())
        for tier in ("Required", "Recommended", "Optional"):
            assert tier in page, f"missing severity tier: {tier}"

    def test_unevaluated_tiers_render_not_assessed_never_green_zero(self) -> None:
        # REQUIRED passed with no should/may issues recorded: the SHOULD/MAY tiers
        # were never evaluated (the build gates at REQUIRED), so they must read
        # "not assessed", never a green "0 issues".
        state = vhps_fixture_state("S-VHPS21")
        page = build_maturity_html(state, validation=self._passed())
        assert "not assessed" in page.lower()
        assert "0 issues" not in page

    def test_recommended_tier_reports_should_issue_count(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        val = self._passed(should_issues=["[Recommended] add a reuse license"])
        page = build_maturity_html(state, validation=val)
        assert "1 issue" in page  # the Recommended tier is now assessed-and-failing
        # Optional was still never evaluated.
        assert "not assessed" in page.lower()

    def test_optional_tier_reports_may_issue_count(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        val = self._passed(may_issues=["[Optional] a", "[Optional] b"])
        page = build_maturity_html(state, validation=val)
        assert "2 issue" in page

    def test_required_tier_failure_reflected(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=False,
            isa_passed=True,
            tox_passed=True,
            required_issues=["root MUST have a name"],
        )
        page = build_maturity_html(state, validation=val)
        # Required tier shows a sub-3 profile count; the failing profile is not a pass.
        assert "Required" in page
        assert "3 / 3 profiles" not in page


class TestOverviewPanel:
    """The All-entities view: the whole crate as one composition map (#85).

    Every other view answers its question by drawing edges. At crate scale (188
    nodes, 79 edges) that renders as a hairball which hides the very composition
    this view exists to show — so it is one tile per entity, clustered by
    category inside its paper layer, with unreachable entities outlined.
    """

    def _graph(self, *, orphan: bool = True) -> dict:
        graph: dict = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "additionalType": "Investigation",
                    "name": "Inv",
                    "hasPart": [{"@id": "#f"}],
                },
                {"@id": "#f", "@type": "File", "name": "result.csv"},
            ]
        }
        if orphan:
            graph["@graph"].append(
                {"@id": "#loose", "@type": "MolecularEntity", "name": "Unwired compound"}
            )
        return graph

    def test_one_tile_per_entity(self) -> None:
        import re

        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph()))
        svg = re.search(r'<svg [^>]*class="prov view overview".*?</svg>', body, re.S)
        assert svg, "overview SVG missing"
        from builder.writers.provenance_dag import build_crate_graph

        entities = [
            n
            for n in build_crate_graph(self._graph(), all_edges=True)["nodes"]
            if n["layer"] is not None
        ]
        assert len(re.findall(r'<rect class="ov-t', svg.group(0))) == len(entities)

    def test_unreachable_entities_are_outlined(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        # `orphan` is the shared base class; the modifier says WHICH kind, and
        # this compound is joined to nothing at all.
        assert 'class="ov-t cat-chemical orphan isolated"' in page
        assert "are unreachable from the crate root" in page

    def test_clean_crate_reports_no_unreachable(self) -> None:
        page = build_maturity_html(
            vhps_fixture_state("S-VHPS21"), graph=self._graph(orphan=False)
        )
        assert "Every entity is reachable from the crate root." in page
        # Pinned to the exact phrase the orphan branch emits, so this stays a
        # real guard rather than passing because the wording moved on.
        assert "are unreachable from the crate root</b>" not in page

    def test_every_tile_names_its_entity(self) -> None:
        # The map summarises; it must not anonymise. Each tile carries the
        # entity's name and type in a tooltip.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert "<title>Unwired compound — MolecularEntity" in page
        assert "linked to nothing at all</title>" in page

    def test_overview_is_the_first_tab_and_selected(self) -> None:
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph()))
        assert body.index('for="mv-all"') < body.index('for="mv-isa"')
        assert 'id="mv-all" checked>' in body

    def test_geometry_stays_inside_the_viewbox(self) -> None:
        import re
        import xml.etree.ElementTree as ET

        def attr(el: ET.Element, name: str) -> str:
            """A geometry attribute the renderer must always emit.

            ``Element.get`` is optional-typed, so a missing coordinate would
            otherwise surface as a bare ``AttributeError: 'NoneType'`` instead of
            naming the element that lost it.
            """
            value = el.get(name)
            assert value is not None, f"<{el.tag}> is missing {name!r}"
            return value

        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph()))
        found = re.search(r'<svg [^>]*class="prov view overview".*?</svg>', body, re.S)
        assert found is not None, "the overview SVG is not in the report"
        root = ET.fromstring(found.group(0))
        _, _, width, height = (float(v) for v in attr(root, "viewBox").split())
        xs: list[float] = []
        ys: list[float] = []
        for el in root.iter():
            if el.tag == "rect":
                x, y = float(attr(el, "x")), float(attr(el, "y"))
                xs += [x, x + float(attr(el, "width"))]
                ys += [y, y + float(attr(el, "height"))]
            elif el.tag == "text":
                xs.append(float(attr(el, "x")))
                ys.append(float(attr(el, "y")))
        assert xs and 0 <= min(xs) and max(xs) <= width
        assert 0 <= min(ys) and max(ys) <= height

    def test_escapes_entity_names(self) -> None:
        graph = self._graph()
        graph["@graph"][-1]["name"] = "<script>alert(1)</script>"
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


class TestAutogeneratedLegend:
    """The provenance legend defines the badge — but only when one is drawn.

    `provenance_dag._display_name` replaces the crate's `AUTOGENERATED — ` name
    prefix with a badge so the filename survives the diagram's 18-character label
    budget. A symbol with no key is a puzzle, so the legend has to carry the
    meaning the words used to.
    """

    @staticmethod
    def _graph(name: str) -> list[dict]:
        return [
            {"@id": "./", "@type": "Dataset"},
            {
                "@id": "#p1",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
                "object": {"@id": "#s1"},
                "result": {"@id": "data/ct.csv"},
            },
            {"@id": "#s1", "@type": "Sample", "name": "CHO-K1 culture"},
            {"@id": "data/ct.csv", "@type": ["File", "csvw:Table"], "name": name},
        ]

    @staticmethod
    def _panel(name: str) -> str:
        from builder.writers.maturity_report import _render_provenance_panel

        return _render_provenance_panel(TestAutogeneratedLegend._graph(name))

    def test_the_badge_is_explained_when_it_is_drawn(self) -> None:
        panel = self._panel("AUTOGENERATED — Condition table")

        assert "generated by this tool" in panel
        assert "not supplied with the deposit" in panel

    def test_the_explanation_sits_in_the_legend(self) -> None:
        """Not merely present in the panel — a caption elsewhere is not a key."""
        import re

        panel = self._panel("AUTOGENERATED — Condition table")
        legend = re.search(r'<div class="prov-legend">(.*?)</div>', panel, re.S)

        assert legend is not None
        assert "generated by this tool" in legend.group(1)

    def test_the_legend_carries_the_same_badge_the_diagram_drew(self) -> None:
        """The key must show the symbol being defined, not a lookalike."""
        from builder.writers.provenance_dag import AUTOGENERATED_BADGE

        panel = self._panel("AUTOGENERATED — Condition table")

        assert panel.count(AUTOGENERATED_BADGE) >= 2, "badge missing from diagram or legend"

    def test_no_explanation_when_no_badge_is_drawn(self) -> None:
        """A legend explaining a symbol the reader cannot find teaches distrust."""
        panel = self._panel("plate_map.csv")

        assert "generated by this tool" not in panel

    def test_the_other_legend_entries_survive(self) -> None:
        """The conditional entry is an addition, not a replacement."""
        panel = self._panel("AUTOGENERATED — Condition table")

        for item in ("Process", "Sample / material", "File / table", "produces (result)"):
            assert item in panel, item

    def test_the_badge_is_hidden_from_screen_readers_in_the_legend(self) -> None:
        """The sentence carries the meaning; announcing "warning" first says it twice."""
        panel = self._panel("AUTOGENERATED — Condition table")

        assert 'class="lg-badge" aria-hidden="true"' in panel


class TestUnreachableRepairEstimate:
    """The panel reports links needed, not entities affected.

    Counting unreachable entities overstates the job whenever they are wired to
    each other, and overstates it worst when the crate is most structured — an
    island of thirty mutually-linked entities is one missing link, not thirty.
    """

    @staticmethod
    def _graph(*, island: int = 3, lone: int = 2) -> dict:
        graph: list[dict] = [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "Inv",
                "hasPart": [{"@id": "#f"}],
            },
            {"@id": "#f", "@type": "File", "name": "result.csv"},
        ]
        for i in range(lone):
            graph.append({"@id": f"#lone{i}", "@type": "Person", "name": f"Alone {i}"})
        # A chain of `island` entities referencing each other, root-detached.
        for i in range(island):
            node: dict = {"@id": f"#i{i}", "@type": "File", "name": f"Island {i}"}
            if i + 1 < island:
                node["mentions"] = [{"@id": f"#i{i + 1}"}]
            graph.append(node)
        return {"@graph": graph}

    def _page(self, **kw) -> str:
        return build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph(**kw))

    def test_the_link_count_is_reported_and_beats_the_entity_count(self) -> None:
        page = self._page()

        # 5 unreachable (3 island + 2 lone) but only 3 groups → 3 links.
        assert "<b>3</b> groups" in page
        assert "<b>3 links</b>" in page
        assert "not 5" in page

    def test_both_kinds_are_named_in_the_prose(self) -> None:
        page = self._page()

        assert "are linked to each other but not to the root" in page
        assert "stand entirely alone" in page

    def test_a_bigger_island_does_not_inflate_the_estimate(self) -> None:
        """The property worth reporting: more entities, same repair."""
        page = self._page(island=8)

        assert "<b>3 links</b>" in page, "growing the island must not grow the work"
        assert "not 10" in page

    def test_a_single_group_is_phrased_in_the_singular(self) -> None:
        page = self._page(island=4, lone=0)

        assert "a single group" in page
        assert "<b>one link</b>" in page
        assert "links</b>" not in page.split("a single group")[1][:200]

    def test_the_legend_names_only_the_kinds_present(self) -> None:
        """A key for a tile the reader cannot find misdescribes the crate."""
        both = self._page()
        island_only = self._page(lone=0)

        assert "unreachable · linked to nothing" in both
        assert "linked to each other, not to the root" in both
        assert "unreachable · linked to nothing" not in island_only

    def test_a_clean_crate_claims_no_work(self) -> None:
        page = build_maturity_html(
            vhps_fixture_state("S-VHPS21"),
            graph={
                "@graph": [
                    {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                    {
                        "@id": "./",
                        "@type": "Dataset",
                        "additionalType": "Investigation",
                        "name": "Inv",
                        "hasPart": [{"@id": "#f"}],
                    },
                    {"@id": "#f", "@type": "File", "name": "result.csv"},
                ]
            },
        )

        assert "Every entity is reachable from the crate root." in page
        assert "would reconnect" not in page
