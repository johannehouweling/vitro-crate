"""Tests for the RO-Crate maturity report (#85)."""

from __future__ import annotations

import json
from pathlib import Path

from builder.state import CrateState, ValidationReport
from builder.tools.builder import build_crate, export_crate
from builder.writers.maturity_report import REPORT_FILENAME, build_maturity_html
from tests.fixtures.vhps_golden_crates import vhps_fixture_state


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
        assert "<h2>Chemicals</h2>" in page
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
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "<h2>Chemicals</h2>" not in page
        assert '<span class="eyebrow">Chemicals</span>' not in page

    def test_no_graph_omits_the_section(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        assert "<h2>Chemicals</h2>" not in page

    def test_matrix_is_bounded_with_more_marker(self) -> None:
        graph = {"@graph": [{"@id": "./", "@type": "Dataset"}]}
        for i in range(15):
            graph["@graph"].append(
                {"@id": f"#c{i}", "@type": "MolecularEntity", "name": f"Compound {i:02d}"}
            )
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "+3 more compounds" in page

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
        section = page.split("<h2>Chemicals</h2>", 1)[1].split("</section>", 1)[0]
        assert "<script" not in section.lower()
        assert "src=" not in section and "@import" not in section


class TestSeverityTiers:
    """Profile adherence reported across Required / Recommended / Optional (#306).

    The report must distinguish a tier that was assessed-and-clean from one that
    was never evaluated. The fast in-loop path (``build_and_validate``) gates at
    REQUIRED severity and never populates ``should_issues`` / ``may_issues``, so an
    empty list at those tiers means "not assessed", NOT "0 issues". Rendering an
    unevaluated tier as a green zero would be a false pass.
    """

    def _passed(self, **overrides: object) -> ValidationReport:
        base = {"base_passed": True, "isa_passed": True, "tox_passed": True}
        base.update(overrides)
        return ValidationReport(**base)  # type: ignore[arg-type]

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
