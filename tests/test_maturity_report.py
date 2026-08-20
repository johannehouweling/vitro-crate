"""Tests for the RO-Crate maturity report (#85)."""

from __future__ import annotations

import re

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


def _mit_pct(page: str) -> int:
    """The percentage printed on the FAIR principle 1.3 (MIT coverage) tile."""
    import re

    m = re.search(r'FAIR principle 1\.3.*?<b>(\d+)</b><span class="den">%', page, re.S)
    assert m, "MIT coverage tile shows no percentage"
    return int(m.group(1))


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
        assert int(head.group(1)) == sum(sc["completed"] for sc in mit.module_scores.values())
        assert int(head.group(2)) == sum(sc["total"] for sc in mit.module_scores.values())
        # Documents overlap — one parameter can serve several — so the headline
        # above being the MODULE-bucket sum (never the per-document sum) is the
        # contract; the prose note that once said so was dropped on the owner's
        # call (#606: the bars explain themselves).

    def test_guidance_documents_are_their_own_card(self, tmp_path: Path) -> None:
        """The per-guidance-document bars are a sibling card of the module
        card, not a block under an inline sub-heading inside it: two
        ``<section>``s, the second headed like every other card
        (``sec-h``/``h2``), the module card ending before the first document
        row, and the old ``mit-sub`` sub-heading gone from page and
        stylesheet alike."""
        import re

        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate
        from builder.writers.maturity_report import _CSS_PATH
        from profiles.context import ISA_TOX_CONTEXT

        state = vhps_fixture_state("S-VHPS21")
        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(state, crate, tmp_path, materialize_payload=False)
        graph = crate.metadata.generate()["@graph"]
        page = build_maturity_html(state, graph=graph)

        mit_card = re.search(
            r'<section>\s*<div class="sec-h"><h2>OECD MIT coverage</h2>.*?</section>',
            page,
            re.S,
        )
        assert mit_card, "no OECD MIT coverage card"
        assert "Per guidance document" not in mit_card.group(0)
        docs_card = re.search(
            r'<section>\s*<div class="sec-h"><h2>Per guidance document</h2></div>.*?</section>',
            page,
            re.S,
        )
        assert docs_card, "no Per guidance document card"
        assert 'class="meter stack"' in docs_card.group(0), "document bars left the card"
        assert docs_card.start() >= mit_card.end(), "cards overlap"
        # The module card is the colour key for the document bars, so nothing
        # may come between them.
        assert not page[mit_card.end() : docs_card.start()].strip()
        assert "mit-sub" not in page
        assert "mit-sub" not in _CSS_PATH.read_text(encoding="utf-8")


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


class TestHeaderAndCards:
    """#607 design handoff: accession headline, subhead, and the two header cards."""

    def _state(self) -> CrateState:
        state = vhps_fixture_state("S-VHPS21")
        state.generator.model = "gpt-5.6-luna"
        state.generator.llm_calls = 41
        state.generator.model_seconds = 1080.0
        state.generator.input_tokens = 1242880
        state.generator.output_tokens = 84310
        state.generator.ended_at = "2026-08-19T20:14:00+00:00"
        return state

    def test_the_headline_is_the_accession_and_the_subhead_the_title(self) -> None:
        page = build_maturity_html(self._state())
        assert "<h1>S-VHPS21</h1>" in page
        assert '<p class="subhead">' in page
        assert "vitro-crate maturity report</span>" in page
        assert "<title>S-VHPS21 — vitro-crate maturity report</title>" in page

    def test_a_crate_without_accession_headlines_the_title_without_subhead(self) -> None:
        state = CrateState()
        state.metadata.title = "Some study"
        page = build_maturity_html(state)
        assert "<h1>Some study</h1>" in page
        assert '<p class="subhead">' not in page

    def test_the_study_card_states_not_stated_rather_than_guessing(self) -> None:
        page = build_maturity_html(CrateState())
        card = re.search(r'<div class="hcard">.*?About this study.*?</div>\n', page, re.S)
        assert card
        assert page.count('<span class="not-stated">not stated</span>') >= 3

    def test_the_crate_card_carries_the_generator_facts(self) -> None:
        page = build_maturity_html(self._state())
        assert "gpt-5.6-luna" in page
        assert "41 calls, 18 minutes" in page
        assert "<b>2026-08-19 20:14 UTC</b>" in page
        assert '<span title="input tokens">&darr; 1,242,880</span>' in page
        assert '<span title="output tokens">&uarr; 84,310</span>' in page

    def test_the_crate_card_links_the_tool_and_names_the_gate(self) -> None:
        state = self._state()
        state.generator.url = "https://github.com/johannehouweling/vitro-crate"
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended", "optional"}
        page = build_maturity_html(state, validation=val)
        assert (
            '<a class="lk" href="https://github.com/johannehouweling/vitro-crate">'
            f"vitro-crate {state.generator.version}</a>" in page
        )
        assert '<span class="hlabel">Validation gate</span><b>optional</b>' in page

    def test_the_crate_card_closes_the_report_above_the_references(self) -> None:
        """The About-this-RO-Crate card sits at the foot of the report — after
        the content sections, above the references (review comment: "place
        this above references"), with the footer last — not in the masthead,
        which keeps only the study card."""
        page = build_maturity_html(self._state())
        i_study = page.index('<div class="hcard-h">About this study</div>')
        i_kgrid = page.index('<div class="kgrid">')
        i_crate = page.index('<div class="hcard-h">About this RO-Crate</div>')
        i_refs = page.index('<div class="refs">')
        i_footer = page.index("<footer>")
        assert i_study < i_kgrid, "the study card left the masthead"
        assert i_kgrid < i_crate < i_refs < i_footer, "wrong foot order"

    def test_the_provenance_note_renders_only_with_the_crates_graph(self) -> None:
        """The note claims the figures come from the crate's own metadata — a
        state-only render cannot claim that, so it must not."""
        from builder.writers.maturity_report import _report_id

        state = self._state()
        bare = build_maturity_html(state)
        assert "This report is" not in bare
        graph = {"@graph": [{"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                            {"@id": "./", "@type": "Dataset", "name": "T"}]}
        page = build_maturity_html(state, graph=graph)
        rid = _report_id(state, graph)
        assert re.fullmatch(r"MR-2026-08-19-[0-9a-f]{6}", rid)
        assert f'<code class="hcode">{rid}</code>, generated by vitro-crate at export' in page
        # Reproducible from the same inputs, different for different content.
        assert _report_id(state, graph) == rid
        other = {"@graph": graph["@graph"] + [{"@id": "#x", "@type": "Thing", "name": "x"}]}
        assert _report_id(state, other) != rid

    def test_the_graph_tile_counts_linked_over_total(self) -> None:
        graph = {"@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": "T", "hasPart": [{"@id": "#f"}]},
            {"@id": "#f", "@type": "File", "name": "a.csv"},
            {"@id": "#orphan", "@type": "Sample", "name": "loose"},
        ]}
        page = build_maturity_html(CrateState(), graph=graph)
        tile = re.search(r'<span class="eyebrow">Graph</span></div>(.*?)</article>', page, re.S)
        assert tile
        m = re.search(r'<b>(\d+)</b><span class="den">/ (\d+)</span>', tile.group(1))
        assert m
        linked, total = int(m.group(1)), int(m.group(2))
        assert total == linked + 1, "exactly the orphan is unlinked"
        assert "number linked and retrieved entities" in tile.group(1)
        # No graph, no tile — a count nobody measured is not rendered as 0.
        assert '<span class="eyebrow">Graph</span>' not in build_maturity_html(CrateState())


class TestStudyCardReadsTheGraph:
    """The About-this-study card's whole graph path: contact + ORCID,
    affiliation + ROR, funder, licence link, publication and dataset DOIs in
    one cell, and the root description under the rule."""

    def _graph(self) -> dict:
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "T",
                    "description": "Four in vitro assays across ten compounds.",
                    "contactPoint": {"@id": "#person"},
                    "funder": {"@id": "#funder"},
                    "license": {"@id": "https://creativecommons.org/licenses/by/4.0/"},
                    "identifier": [{"@id": "#doi"}],
                    "citation": [{"@id": "#article"}],
                },
                {
                    "@id": "#person",
                    "@type": "Person",
                    "name": "Nathalie Dierichs",
                    "identifier": [{"@id": "#orcid"}],
                    "affiliation": {"@id": "#org"},
                },
                {"@id": "#orcid", "@type": "PropertyValue", "name": "ORCID",
                 "value": "0009-0000-5074-6239"},
                {"@id": "#org", "@type": "Organization", "name": "RIVM",
                 "identifier": [{"@id": "#ror"}]},
                {"@id": "#ror", "@type": "PropertyValue", "name": "ROR", "value": "01cesdt21"},
                {"@id": "#funder", "@type": "Organization", "name": "ZonMw",
                 "url": "https://www.zonmw.nl/"},
                {"@id": "https://creativecommons.org/licenses/by/4.0/",
                 "@type": "CreativeWork"},
                {"@id": "#doi", "@type": "PropertyValue", "name": "DOI",
                 "value": "10.21945/S-VHPS22"},
                {"@id": "#article", "@type": "ScholarlyArticle",
                 "name": "Thyroid disruption in vitro",
                 "identifier": "https://doi.org/10.1016/j.envint.2025.108000"},
            ]
        }

    @staticmethod
    def _card(page: str) -> str:
        """The About-this-study card — the masthead's only card, so it runs
        from its heading to the KPI grid that follows the masthead."""
        body = page.split("</style>", 1)[-1]
        i = body.index('<div class="hcard-h">About this study</div>')
        return body[i : body.index('<div class="kgrid">', i)]

    def test_every_cell_states_the_graphs_own_fact(self) -> None:
        page = build_maturity_html(CrateState(), graph=self._graph())
        card = self._card(page)
        assert '<a class="lk" href="https://orcid.org/0009-0000-5074-6239">' in card
        assert "Nathalie Dierichs</a>" in card
        assert '<a class="lk" href="https://ror.org/01cesdt21">RIVM</a>' in card
        assert '<a class="lk" href="https://www.zonmw.nl/">ZonMw</a>' in card
        # A bare licence URL resolves to the registry's own name, never the
        # URL's last path segment.
        assert "Creative Commons Attribution 4.0 International" in card
        assert "legalcode" not in card and ">4.0<" not in card
        assert "not stated" not in card, "every field is stated in this graph"
        assert "Four in vitro assays across ten compounds." in card

    def test_the_publication_and_dataset_dois_share_one_cell(self) -> None:
        """The handoff pins this: pinning them to grid columns broke their
        pairing every time a field was added."""
        card = self._card(build_maturity_html(CrateState(), graph=self._graph()))
        cell = re.search(
            r'<div class="hcell"><span class="hlabel">Publication</span>(.*?)</div>', card, re.S
        )
        assert cell, "no publication cell"
        assert '<a class="lk" href="https://doi.org/10.1016/j.envint.2025.108000">' in cell.group(1)
        assert "doi.org/10.1016/j.envint.2025.108000</a>" in cell.group(1), "DOI must be a link"
        assert '<span class="hlabel">Dataset</span>' in cell.group(1)
        assert "doi.org/10.21945/S-VHPS22" in cell.group(1)

    def test_the_subhead_prefers_the_publication_name(self) -> None:
        state = CrateState()
        state.metadata.title = "The study title"
        page = build_maturity_html(state, graph=self._graph())
        assert '<p class="subhead">Thyroid disruption in vitro</p>' in page
        # The h1 is the title only because this state has no accession; the
        # SUBHEAD is the publication, never a second copy of the title.
        assert page.count("The study title") == 2  # <title> and <h1>

    def test_a_bare_string_contact_is_not_reported_as_not_stated(self) -> None:
        graph = {"@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": "T", "creator": "Jane Doe"},
        ]}
        card = self._card(build_maturity_html(CrateState(), graph=graph))
        assert "Jane Doe" in card


class TestLinkGuard:
    """#607: only http(s) targets ever become an href — a URL in a card is
    crate-controlled text, so ``javascript:`` must render as inert words."""

    def test_lk_refuses_non_web_schemes(self) -> None:
        from builder.writers.maturity_report import _lk

        for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://h/f", "#frag"):
            out = _lk(bad, "label")
            assert "<a" not in out and "href" not in out, bad
            assert out == "label"
        good = _lk("https://example.org/x", "label")
        assert good == '<a class="lk" href="https://example.org/x">label</a>'

    def test_an_affiliation_url_from_the_graph_cannot_smuggle_a_scheme(self) -> None:
        """The page-level path: an Organization whose @id is a javascript: URL
        reaches _lk through the affiliation cell."""
        graph = {"@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": "T", "contactPoint": {"@id": "#p"}},
            {"@id": "#p", "@type": "Person", "name": "P",
             "affiliation": {"@id": "javascript:alert(1)//org"}},
            {"@id": "javascript:alert(1)//org", "@type": "Organization", "name": "Evil"},
        ]}
        page = build_maturity_html(CrateState(), graph=graph)
        assert 'href="javascript:' not in page
        assert "Evil" in page, "the name is still reported, just not as a link"

    def test_every_card_url_reaches_the_guard(self) -> None:
        """The three unfiltered paths into ``_lk``: an affiliation @id, a
        licence node's own ``url`` property, and the generator's homepage."""
        state = CrateState()
        state.generator.url = "javascript:alert(9)"
        graph = {"@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": "T", "license": {"@id": "#lic"}},
            {"@id": "#lic", "@type": "CreativeWork", "name": "Bad licence",
             "url": "javascript:alert(5)"},
        ]}
        page = build_maturity_html(state, graph=graph)
        assert "javascript:" not in page.split("</style>", 1)[-1]
        assert "Bad licence" in page and state.generator.name in page


class TestProfileConformanceMatrix:
    """The KPI matrix: rows the three layers linked to their specs, cells the
    per-tier state with the finding count on the title."""

    def test_the_spec_urls_are_the_crates_own_conformsTo_iris(self) -> None:
        """The pin the registry comment promises: the matrix links exactly the
        IRIs the crate declares (`_crate_mapping`'s constants)."""
        from builder.tools._crate_mapping import PROFILE_ISA, PROFILE_ISATOX, ROCRATE_SPEC
        from builder.writers.maturity_report import _PROFILE_SPEC_URLS

        assert _PROFILE_SPEC_URLS == {
            "base": ROCRATE_SPEC,
            "isa": PROFILE_ISA,
            "tox": PROFILE_ISATOX,
        }

    def _verdict(self) -> ValidationReport:
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=False)
        val.assessed_tiers = {"required", "recommended"}
        val.issue_records = [
            {"profile": "tox", "severity": "required", "entity_id": "#a", "message": "m1"},
            {"profile": "isa", "severity": "recommended", "entity_id": "#b", "message": "m2"},
            {"profile": "isa", "severity": "recommended", "entity_id": "#c", "message": "m3"},
        ]
        return val

    def _cells(self, page: str) -> dict[str, tuple[str, str]]:
        return {
            m.group(1): (m.group(3), m.group(2))
            for m in re.finditer(
                r'data-cell="([\w-]+)" title="([^"]*)"[^>]*><span class="mk (ok|no|na)"', page
            )
        }

    def test_cells_follow_the_verdict_and_titles_carry_the_counts(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        cells = self._cells(build_maturity_html(state, validation=self._verdict()))
        assert cells["base-required"] == ("ok", "no findings at this level")
        assert cells["isa-required"] == ("ok", "no findings at this level")
        assert cells["tox-required"] == ("no", "1 finding at this level")
        assert cells["isa-recommended"] == ("no", "2 findings at this level")
        assert cells["base-recommended"] == ("ok", "no findings at this level")
        # The optional tier was never evaluated: neutral, never a green zero.
        for profile in ("base", "isa", "tox"):
            assert cells[f"{profile}-optional"][0] == "na"

    def test_a_stale_verdict_turns_every_cell_neutral(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        val = self._verdict()
        val.input_fingerprint = "not-this-state"
        cells = self._cells(build_maturity_html(state, validation=val))
        assert {c[0] for c in cells.values()} == {"na"}

    def test_a_failed_gate_with_no_findings_still_reads_as_a_fail(self) -> None:
        """A verdict can fail a profile's required gate and carry no findings
        (a crashed or truncated validator run). The cell must say so — a green
        there would be the false green this matrix exists to refuse."""
        val = ValidationReport(base_passed=False, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required"}
        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))
        assert cells["base-required"] == ("no", "profile gate failed")
        assert cells["isa-required"][0] == "ok"

    def test_a_pre_records_verdict_still_counts_by_profile(self) -> None:
        """A checkpoint written before ``issue_records`` existed carries only
        flat display strings — their own ``[profile]`` prefix is what the
        matrix counts, so the titles stay true rather than claiming zero."""
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended"}
        val.should_issues = ["[isa] #a: SHOULD have x", "[isa] #b: SHOULD have y",
                             "[base] ./: SHOULD have z"]
        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))
        assert cells["isa-recommended"] == ("no", "2 findings at this level")
        assert cells["base-recommended"] == ("no", "1 finding at this level")
        assert cells["tox-recommended"] == ("ok", "no findings at this level")

    def test_findings_with_no_profile_get_their_own_row(self) -> None:
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended"}
        val.issue_records = [
            {"profile": "", "severity": "recommended", "entity_id": "#x", "message": "m"},
        ]
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val)
        cells = self._cells(page)
        assert "unattributed" in page
        assert cells["unattributed-recommended"] == (
            "no",
            "1 finding not attributed to a profile",
        )

    def test_an_unvalidated_state_renders_all_neutral(self) -> None:
        cells = self._cells(build_maturity_html(CrateState()))
        assert {c[0] for c in cells.values()} == {"na"}
        assert {c[1] for c in cells.values()} == {"not yet validated"}


class TestFairTileAndRose:
    """The FAIR ladder's partial next rung + blockers line, and the MIT rose."""

    def test_the_next_rung_is_filled_to_the_indicator_ratio(self) -> None:
        from builder.tools.fair_assessment import assess_fair_maturity, dsm_blockers
        from builder.tools.mit_assessment import assess_mit_coverage

        state = vhps_fixture_state("S-VHPS21")
        mit = assess_mit_coverage(state)
        fair = assess_fair_maturity(state, mit=mit)
        met = sum(1 for i in fair.indicator_results if i.get("passed") is True)
        assessed = sum(1 for i in fair.indicator_results if i.get("passed") is not None)
        page = build_maturity_html(state)
        assert f"<b>{fair.dsm_level}</b>" in page
        assert page.count('<span class="rung2 done"></span>') == fair.dsm_level
        next_rung = re.search(r'<span class="rung2 next"[^>]*><i style="width:(\d+)%">', page)
        assert next_rung and int(next_rung.group(1)) == round(met / assessed * 100)
        blockers = dsm_blockers(state)
        assert blockers, "fixture has no DSM blockers; the assertion below is inert"
        assert (
            f"<b>{len(blockers)} indicator{'s' if len(blockers) != 1 else ''}</b> "
            f"to level {fair.dsm_level + 1}" in page
        )
        # The count is drillable: the fold names every blocking indicator.
        for bid, text in blockers:
            assert f"<code>{bid}</code>" in page and text in page, bid

    def test_the_rose_draws_every_module_to_the_scorers_numbers(self) -> None:
        import math

        from builder.tools.mit_assessment import assess_mit_coverage
        from builder.writers.maturity_report import _mit_rose_svg

        state = vhps_fixture_state("S-VHPS21")
        mit = assess_mit_coverage(state)
        svg = _mit_rose_svg(mit)
        drawn = {m for m in mit.module_scores if mit.module_scores[m]["completed"]}
        # Every module with fields is one hoverable group carrying its caption,
        # whether or not anything is filled in it.
        for name, sc in mit.module_scores.items():
            if sc["total"]:
                assert f"<title>{name} — {sc['completed']} of {sc['total']} filled</title>" in svg
        # Background wedges: one per module with fields; they tile the circle.
        assert svg.count('fill="#f2f5f5"') == sum(
            1 for sc in mit.module_scores.values() if sc["total"]
        )
        # A known wedge's radius is 82 × its fill fraction: check via the arc.
        name = next(iter(drawn))
        sc = mit.module_scores[name]
        expected_r = 82 * sc["completed"] / sc["total"]
        group = re.search(
            rf'<g class="rw"><title>{re.escape(name)} —.*?</g>', svg, re.S
        )
        assert group, name
        radii = re.findall(r'A ([\d.]+),[\d.]+ 0 [01] 1 ', group.group(0))
        assert len(radii) == 2, "a share wedge and a filled wedge"
        assert math.isclose(float(radii[0]), 82.0, abs_tol=0.01), "the share wedge is full radius"
        assert math.isclose(float(radii[1]), expected_r, abs_tol=0.01)

    def test_each_wedges_angle_is_its_modules_share(self) -> None:
        """The other half of the encoding: angle = the module's share of the
        checklist (radius = its fill). Equal slices would pass a radius-only
        test, so the sweep is measured from the arc endpoints."""
        import math

        from builder.tools.mit_assessment import assess_mit_coverage
        from builder.writers.maturity_report import _mit_rose_svg

        mit = assess_mit_coverage(vhps_fixture_state("S-VHPS21"))
        total_all = sum(sc["total"] for sc in mit.module_scores.values())
        svg = _mit_rose_svg(mit)
        # Background wedges are full-radius and in module order.
        paths = re.findall(
            r'M 87\.0,87\.0 L ([\d.]+),([\d.]+) A 82\.00,82\.00 0 [01] 1 ([\d.]+),([\d.]+) Z"'
            r' fill="#f2f5f5"',
            svg,
        )  # the pale share wedges, one per module, in module order
        drawn = [(n, sc) for n, sc in mit.module_scores.items() if sc["total"]]
        assert len(paths) == len(drawn)

        def angle(x: str, y: str) -> float:
            return math.degrees(math.atan2(float(y) - 87.0, float(x) - 87.0)) + 90

        for (x0, y0, x1, y1), (name, sc) in zip(paths, drawn, strict=True):
            sweep = (angle(x1, y1) - angle(x0, y0)) % 360
            assert math.isclose(sweep, sc["total"] / total_all * 360, abs_tol=0.05), name

    def test_each_module_is_one_hoverable_group_with_its_label(self) -> None:
        """Hover names the module: each slice is a ``<g>`` holding its share
        wedge, its filled wedge, the caption and a label the stylesheet
        reveals on hover — and the stylesheet must actually reveal it."""
        from builder.tools.mit_assessment import assess_mit_coverage
        from builder.writers.maturity_report import _CSS_PATH, _mit_rose_svg

        mit = assess_mit_coverage(vhps_fixture_state("S-VHPS21"))
        svg = _mit_rose_svg(mit)
        groups = re.findall(r'<g class="rw">.*?</g>', svg, re.S)
        assert len(groups) == sum(1 for sc in mit.module_scores.values() if sc["total"])
        for group, (name, sc) in zip(
            groups, [(n, s) for n, s in mit.module_scores.items() if s["total"]], strict=True
        ):
            assert f'<text class="rw-l"' in group, name
            assert f'{sc["completed"]} of {sc["total"]} filled</tspan>' in group, name
            # A long name is wrapped, not clipped by the viewBox.
            for line in re.findall(r'<tspan x="87.0" dy="\d+">([^<]*)</tspan>', group):
                assert len(line) <= 22, (name, line)
        css = _CSS_PATH.read_text(encoding="utf-8")
        assert ".mat .rose-wrap .rw:hover .rw-l { opacity:1; }" in css
        assert "svg:hover .rw:not(:hover) path" in css

    def test_the_footnote_superscripts_resolve(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        for fn in ("fn-dsm", "fn-mit"):
            assert f'href="#{fn}"' in page and f'id="{fn}"' in page

    def test_the_references_cite_what_the_assessors_actually_implement(self) -> None:
        """The handoff flagged this citation as the thing to confirm: the DSM
        note must name the model ``fair/dsm_indicators.yaml`` implements (the
        FAIRplus DSM, derived from the RDA FDMM), and the MIT note the
        indicator package the checklist comes from."""
        from builder.tools.fair_assessment import DSM_INDICATORS_PATH
        from builder.tools.mit_assessment import MIT_INDICATORS_URL

        yaml_text = DSM_INDICATORS_PATH.read_text(encoding="utf-8")
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        refs = page[page.index('<div class="refs">') :]
        for url in ("https://fairplus.github.io/Data-Maturity/", "https://doi.org/10.15497/rda00050"):
            assert f'href="{url}"' in refs, url
            assert url.rstrip("/").split("//", 1)[1] in yaml_text.replace("doi:", "doi.org/"), (
                f"{url} is not the source the YAML cites"
            )
        assert "Levels are gated" in refs
        # The note must NAME both models, not merely link them: the link text
        # is a DOI, and a reader cannot tell which maturity model was scored.
        assert "FAIRplus Dataset Maturity (DSM) model" in refs
        assert "RDA FAIR Data Maturity Model" in refs
        assert f'href="{MIT_INDICATORS_URL}"' in refs
        assert "principle R1.3" in refs


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

    def test_fresh_verdict_still_reports_conformant(self) -> None:
        from tests.fixtures.report import profile_verdict

        assert profile_verdict(build_maturity_html(self._validated())) == "ok"

    def test_edited_after_validating_is_reported_out_of_date(self) -> None:
        from tests.fixtures.report import profile_verdict

        state = self._validated()
        state.metadata.title = "Edited after validating"
        page = build_maturity_html(state)
        assert profile_verdict(page) == "na"
        assert "out of date" in page

    def test_stale_report_makes_no_pass_claim_anywhere(self) -> None:
        from tests.fixtures.report import profile_verdict

        state = self._validated()
        state.metadata.title = "Edited after validating"
        page = build_maturity_html(state)
        assert profile_verdict(page) != "ok"
        # The tier summary asserts a pass as loudly as a green tick.
        assert "3 / 3 profiles" not in page
        assert "out of date" in page
        assert "Re-run validation" in page

    def test_unstamped_verdict_is_trusted_not_flagged(self) -> None:
        from tests.fixtures.report import profile_verdict

        # A checkpoint written before the stamp existed must not be downgraded.
        state = vhps_fixture_state("S-VHPS21")
        state.validation = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        assert profile_verdict(build_maturity_html(state)) == "ok"


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
        assert "out of date" not in page

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
        from tests.fixtures.report import profile_verdict

        page = (out / REPORT_FILENAME).read_text(encoding="utf-8")
        assert profile_verdict(page) == "no"
        assert "root MUST have a name" in page

    def test_validator_failure_does_not_fail_the_export(self, monkeypatch, tmp_path: Path) -> None:
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
        res = export_crate(vhps_fixture_state("S-VHPS21"), str(tmp_path / "c"), validate=False)
        assert res["success"] is True
        assert "validation" not in res
        assert calls == []


class TestProvenanceSection:
    """When a crate ``@graph`` is supplied, the report folds in the graph
    views, LabProcesses' derivation-chain SVG among them (#85). The topology
    strip that once travelled with it was removed on the owner's review."""

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

    def test_graph_renders_provenance(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        page = build_maturity_html(state, graph=self._chain_graph())
        assert "LabProcesses" in page  # the view's tab label (renamed, owner's call)
        assert 'class="prov"' in page  # the inline derivation-chain SVG
        assert "result.csv" in page

    def test_no_graph_omits_provenance_section(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        assert 'class="prov"' not in page

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

        with_graph = _mit_pct(build_maturity_html(state, graph=graph))
        assert with_graph > 0
        # HONESTY CONTROL (#311): omitting the graph must not change the number.
        # It used to: the report fell back to a second scorer that credited
        # nothing and printed "MIT coverage 0%" for this same crate — a false
        # statement, not a cheap approximation. The assessor now assembles its
        # own graph, so there is one score per crate however it is reached.
        assert _mit_pct(build_maturity_html(state)) == with_graph

    def test_export_embeds_provenance_from_crate_graph(self, tmp_path: Path) -> None:
        # End-to-end: the embedded report is built with the crate's real @graph,
        # so the graph views travel with the written crate.
        state = vhps_fixture_state("S-VHPS21")
        out = tmp_path / "crate"
        state.metadata.output_path = str(out)
        build_crate(state)
        page = (out / REPORT_FILENAME).read_text(encoding="utf-8")
        assert "<h2>Graph views</h2>" in page
        assert 'class="prov' in page


class TestMitModuleColours:
    """#606: one MIT module, one colour — and each guidance-document bar is
    split into those modules, each module's share a small progress bar of its
    own (solid = filled, pale = still missing), so a reader sees how far a
    document is covered and which module the remaining gaps live in.

    The registry ``MIT_MODULE_STYLES`` (``maturity_report.py``) is THE one place
    a module colour is written — the ``CATEGORY_STYLES`` rule (#487) applied to
    the checklist. The renderer draws what the scorer counted: every span of a
    document's bar is one ``standard_module_scores`` bucket, so the assertions
    below compare the rendered widths/titles against the scorer's own numbers,
    never against values read off the page.
    """

    @staticmethod
    def _scored(tmp_path: Path):
        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate
        from builder.tools.mit_assessment import assess_mit_coverage
        from profiles.context import ISA_TOX_CONTEXT

        state = vhps_fixture_state("S-VHPS21")
        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(state, crate, tmp_path, materialize_payload=False)
        graph = crate.metadata.generate()["@graph"]
        return assess_mit_coverage(state, graph=graph), build_maturity_html(state, graph=graph)

    @staticmethod
    def _mit_section(page: str) -> str:
        m = re.search(r"<h2>OECD MIT coverage</h2>.*?</section>", page, re.S)
        assert m, "no MIT section"
        return m.group(0)

    @staticmethod
    def _docs_section(page: str) -> str:
        m = re.search(r"<h2>Per guidance document</h2>.*?</section>", page, re.S)
        assert m, "no per-guidance-document section"
        return m.group(0)

    @staticmethod
    def _row(section: str, label: str) -> str:
        """The ``mrow`` whose name cell is *label* (exactly)."""
        m = re.search(
            r'<div class="mrow"[^>]*>\s*<div class="mname">' + re.escape(label) + r"</div>.*?"
            r'<div class="mfrac">.*?</div>\s*</div>',
            section,
            re.S,
        )
        assert m, f"no row for {label}"
        return m.group(0)

    def test_every_shipped_module_has_a_colour(self) -> None:
        """Drift guard: the registry names exactly the shipped checklist's six
        modules — a renamed or added module would otherwise silently fall back
        to grey."""
        from builder.tools.mit_assessment import load_mit_yaml
        from builder.writers.maturity_report import MIT_MODULE_STYLES

        mit_data = load_mit_yaml()
        assert mit_data is not None
        assert set(MIT_MODULE_STYLES) == {m["name"] for m in mit_data["modules"]}
        assert len(MIT_MODULE_STYLES) == 6
        colours = list(MIT_MODULE_STYLES.values())
        assert len(set(colours)) == len(colours), "two modules share a colour"
        for colour in colours:
            assert re.fullmatch(r"#[0-9a-f]{6}", colour), colour

    def test_colours_are_far_enough_apart_and_read_on_the_page(self) -> None:
        """The bars are 8px tall, so colour is most of the signal: every pair
        clears CIE76 dE 20 (the category-palette floor), every colour clears
        3:1 on white, and none sits within dE 12 of a status colour — read from
        the stylesheet's own tokens — so a module cannot impersonate a verdict."""
        import itertools

        from builder.writers.maturity_report import _CSS_PATH, MIT_MODULE_STYLES
        from tests.fixtures.colour import ciede as _ciede
        from tests.fixtures.colour import contrast_on_white as _contrast_on_white

        for (name_a, a), (name_b, b) in itertools.combinations(MIT_MODULE_STYLES.items(), 2):
            assert _ciede(a, b) >= 20, f"{name_a} vs {name_b}: dE {_ciede(a, b):.1f}"
        status = dict(
            re.findall(r"--(good|warn|low|cov):(#[0-9a-f]{6})", _CSS_PATH.read_text("utf-8"))
        )
        assert set(status) == {"good", "warn", "low", "cov"}, "status tokens moved"
        for name, colour in MIT_MODULE_STYLES.items():
            assert _contrast_on_white(colour) >= 3.0, name
            for verdict, s in status.items():
                assert _ciede(colour, s) >= 12, f"{name} reads as {verdict}"

    def test_colours_stay_apart_for_dichromat_readers(self) -> None:
        """Every pair — any two can touch, since a module with nothing in a
        document drops out of its bar — clears OKLab dE 8 (×100, the
        categorical-palette target) under simulated protanopia and
        deuteranopia (Machado, Oliveira & Fernandes 2009, severity 1.0) and dE
        15 under normal vision. Hue alone cannot do this for six colours; the
        registry alternates lightness to, and this pins that it does."""
        import itertools
        import math

        from builder.writers.maturity_report import MIT_MODULE_STYLES

        def linear(colour: str) -> tuple[float, float, float]:
            def chan(c: float) -> float:
                return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

            r, g, b = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
            return chan(r), chan(g), chan(b)

        def oklab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
            r, g, b = rgb
            l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
            m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
            s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
            return (
                0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
                1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
                0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
            )

        # Machado et al. 2009, severity 1.0, applied in linear RGB.
        simulations = {
            "normal": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
            "protan": (
                (0.152286, 1.052583, -0.204868),
                (0.114503, 0.786281, 0.099216),
                (-0.003882, -0.048116, 1.051998),
            ),
            "deutan": (
                (0.367322, 0.860646, -0.227968),
                (0.280085, 0.672501, 0.047413),
                (-0.011820, 0.042940, 0.968881),
            ),
        }

        def seen_as(colour: str, kind: str) -> tuple[float, float, float]:
            rgb = linear(colour)
            r, g, b = (
                min(1.0, max(0.0, sum(m * c for m, c in zip(row, rgb, strict=True))))
                for row in simulations[kind]
            )
            return oklab((r, g, b))

        floors = {"normal": 15.0, "protan": 8.0, "deutan": 8.0}
        for kind, floor in floors.items():
            for (name_a, a), (name_b, b) in itertools.combinations(MIT_MODULE_STYLES.items(), 2):
                d = 100 * math.dist(seen_as(a, kind), seen_as(b, kind))
                assert d >= floor, f"{name_a} vs {name_b} under {kind}: dE {d:.1f} < {floor}"

    def test_stylesheet_declares_no_module_colour(self) -> None:
        """The registry is the only place a module colour is written."""
        from builder.writers.maturity_report import _CSS_PATH, MIT_MODULE_STYLES

        css = _CSS_PATH.read_text(encoding="utf-8").lower()
        for name, colour in MIT_MODULE_STYLES.items():
            assert colour not in css, name

    def test_stylesheet_draws_every_module_state_from_the_one_token(self) -> None:
        """The rules that turn ``--mod`` into a bar exist and take their colour
        from that token alone — without them the spans are unstyled inline
        elements and the split is invisible while every markup test stays
        green (the #487 "a category with no rules renders as an unstyled box"
        lesson)."""
        from builder.writers.maturity_report import _CSS_PATH

        css = _CSS_PATH.read_text(encoding="utf-8")

        def rule(selector: str) -> str:
            m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
            assert m, f"no rule for {selector}"
            return m.group(1)

        # Declared on the elements that carry --mod: a custom property resolves
        # its var() where it is declared, so a hoisted token is invalid everywhere.
        assert "--mod-pale:color-mix(in srgb,var(--mod)" in rule(".mat .mrow, .mat .meter.stack > .mod")
        assert "background:var(--mod-pale)" in rule(".mat .meter.mod")
        assert "background:var(--mod)" in rule(".mat .fill-mod")
        assert "display:flex" in rule(".mat .meter.stack") and "gap:" in rule(".mat .meter.stack")
        pill = rule(".mat .meter.stack > .mod")
        assert "display:flex" in pill and "flex:1 1 0" in pill and "border-radius:999px" in pill
        assert "background:var(--mod)" in rule(".mat .meter.stack .seg")
        assert "background:var(--mod-pale)" in rule(".mat .meter.stack .seg.pale")
        assert "print-color-adjust:exact" in rule(
            ".mat .meter, .mat .meter *, .mat .rung2, .mat .rung2 > i, .mat .dot"
        )
        mit_block = css[css.index("/* MIT */") : css.index("/* profile detail */")]
        rules_only = re.sub(r"/\*.*?\*/", "", mit_block, flags=re.S)  # "#606" is an issue, not a hex
        assert not re.search(r"#[0-9a-f]{3,6}\b", rules_only), "MIT rules hardcode a colour"

    def test_module_rows_wear_their_own_colour_in_the_checklists_order(
        self, tmp_path: Path
    ) -> None:
        """Each module row carries its colour as ``--mod`` (fill solid, track
        pale — the same two states the document bars use), and the rows follow
        the scorer's own module order, which is the checklist's."""
        from builder.writers.maturity_report import MIT_MODULE_STYLES

        mit, page = self._scored(tmp_path)
        section = self._mit_section(page)
        seen_at: list[int] = []
        for name, sc in mit.module_scores.items():
            row = self._row(section, name)
            assert f'<div class="mrow" style="--mod:{MIT_MODULE_STYLES[name]}">' in row, name
            fill = re.search(
                r'<div class="meter mod" role="img" aria-label="(\d+) of (\d+)">'
                r'<i class="fill-mod" style="width:([\d.]+)%"></i></div>',
                row,
            )
            assert fill, name
            assert (int(fill.group(1)), int(fill.group(2))) == (sc["completed"], sc["total"])
            assert abs(float(fill.group(3)) - sc["completed"] / sc["total"] * 100) < 0.6, name
            seen_at.append(section.index(row))
        assert seen_at == sorted(seen_at), "module rows are not in the scorer's order"

    def test_document_bars_are_split_by_module_each_filled_then_missing(
        self, tmp_path: Path
    ) -> None:
        """A document's bar is one pill per contributing module, in checklist
        order, sized by that module's share of the document (its field count
        as flex-grow); inside each pill the filled part is solid and the
        missing part pale, so the pill is the module's own progress bar.
        Shares and titles are the scorer's numbers; the pills sum to the
        document's total; the bar's accessible name carries the same numbers."""
        import html as _html

        from builder.tools.mit_assessment import MIT_STANDARD_LABELS
        from builder.writers.maturity_report import MIT_MODULE_STYLES

        mit, page = self._scored(tmp_path)
        section = self._docs_section(page)
        assert mit.standard_module_scores
        span_re = re.compile(
            r'<span class="mod" style="--mod:(?P<colour>#[0-9a-f]{6});flex-grow:(?P<w>\d+)">'
            r"(?P<inner>.*?)</span>",
            re.S,
        )
        seg_re = re.compile(
            r'<i class="seg(?P<pale> pale)?" style="width:(?P<w>[\d.]+)%" '
            r'title="(?P<title>[^"]+)"></i>'
        )
        docs_with_two_modules = 0
        docs_with_both_states = 0
        for key, by_module in mit.standard_module_scores.items():
            label = MIT_STANDARD_LABELS[key]
            row = self._row(section, label)
            doc = mit.standard_scores[key]
            order = [m for m in mit.module_scores if m in by_module]
            described = ", ".join(
                f"{m} {by_module[m]['completed']} of {by_module[m]['total']}" for m in order
            )
            assert (
                f'<div class="meter stack" role="img" '
                f'aria-label="{doc["completed"]} of {doc["total"]}: {_html.escape(described)}">'
            ) in row, label
            spans = span_re.findall(row)
            assert [c for c, _w, _i in spans] == [MIT_MODULE_STYLES[m] for m in order], label
            # The shares are the field counts themselves, so the pills sum to
            # the document's total by construction (no percentage rounding).
            assert sum(int(w) for _c, w, _i in spans) == doc["total"], label
            for (_colour, width, inner), m in zip(spans, order, strict=True):
                b = by_module[m]
                assert int(width) == b["total"], (label, m)
                segments = seg_re.findall(inner)
                expected = []
                if b["completed"]:
                    expected.append(
                        ("", b["completed"] / b["total"] * 100, f"{m}: {b['completed']} of {b['total']} filled")
                    )
                missing = b["total"] - b["completed"]
                if missing:
                    expected.append(
                        (" pale", missing / b["total"] * 100, f"{m}: {missing} of {b['total']} still missing")
                    )
                assert len(segments) == len(expected), (label, m)
                for (pale, w, title), (e_pale, e_w, e_title) in zip(segments, expected, strict=True):
                    assert pale == e_pale, (label, m)
                    assert abs(float(w) - e_w) < 0.01, (label, m)
                    assert _html.unescape(title) == e_title, (label, m)
            if len(spans) >= 2:
                docs_with_two_modules += 1
            if any(" pale" in i and 'class="seg"' in i for _c, _w, i in spans):
                docs_with_both_states += 1
        # Non-vacuity: the fixture really exercises the split and both states.
        assert docs_with_two_modules >= 1
        assert docs_with_both_states >= 1

    def test_the_section_carries_one_sentence_of_prose(self, tmp_path: Path) -> None:
        """The user's call: the bars explain themselves. One lead sentence
        naming the indicators, no legend, no lead under the sub-heading."""
        from builder.tools.mit_assessment import MIT_INDICATORS_URL

        _mit, page = self._scored(tmp_path)
        section = self._mit_section(page)
        leads = re.findall(r'<p class="lead">(.*?)</p>', section, re.S)
        assert leads == [
            "Coverage of the in-vitro toxicology MIT checklist — each item is a FAIR maturity "
            f'indicator as defined in <a href="{MIT_INDICATORS_URL}">tox-maturity-indicators</a>.'
        ]
        assert 'class="mit-key"' not in section and "<legend" not in section

    def test_section_names_the_indicators_it_scores(self, tmp_path: Path) -> None:
        """The checklist items are FAIR maturity indicators as defined in
        tox-maturity-indicators — the section says so and links the definition."""
        from builder.tools.mit_assessment import MIT_INDICATORS_URL

        _mit, page = self._scored(tmp_path)
        section = self._mit_section(page)
        assert MIT_INDICATORS_URL == "https://github.com/invitro-crate/tox-maturity-indicators"
        assert f'href="{MIT_INDICATORS_URL}"' in section
        assert "maturity indicator" in section.lower()

    def test_an_unknown_module_is_drawn_grey_not_dropped(self) -> None:
        """A module the registry does not know (a renamed checklist) still gets
        its row and its span, in the neutral fallback colour; a module a
        document's split names that has no row of its own is still drawn
        (after the ones that do); a zero-total bucket draws nothing and divides
        by nothing; and every name reaches the page escaped — a report rebuilt
        from session JSON can carry any string."""
        import html as _html

        from builder.state import MITReport
        from builder.writers.maturity_report import (
            MIT_MODULE_FALLBACK_COLOUR,
            MIT_MODULE_STYLES,
            _render_mit_section,
        )

        known = next(iter(MIT_MODULE_STYLES))
        odd = 'Zeta "module" <b>'
        report = MITReport(
            module_scores={
                known: {"completed": 1, "total": 4},
                odd: {"completed": 1, "total": 2},
            },
            overall_score=2 / 6,
            standard_scores={"oecd_gd211": {"completed": 2, "total": 6}},
            standard_module_scores={
                "oecd_gd211": {
                    "Empty bucket": {"completed": 0, "total": 0},
                    "Only in the split": {"completed": 0, "total": 1},
                    odd: {"completed": 1, "total": 2},
                    known: {"completed": 1, "total": 4},
                }
            },
        )
        section = _render_mit_section(report)
        assert MIT_MODULE_FALLBACK_COLOUR not in MIT_MODULE_STYLES.values()
        assert odd not in section, "a module name reached the page unescaped"
        esc = _html.escape(odd)
        zeta = self._row(section, esc)
        assert f'<div class="mrow" style="--mod:{MIT_MODULE_FALLBACK_COLOUR}">' in zeta
        doc = self._row(section, "OECD GD 211")
        described = f"{known} 1 of 4, {odd} 1 of 2, Only in the split 0 of 1"
        assert f'aria-label="2 of 6: {_html.escape(described)}"' in doc
        spans = re.findall(r'<span class="mod" style="--mod:([^;]+);flex-grow:(\d+)">', doc)
        assert spans == [
            (MIT_MODULE_STYLES[known], "4"),
            (MIT_MODULE_FALLBACK_COLOUR, "2"),
            (MIT_MODULE_FALLBACK_COLOUR, "1"),
        ]
        assert f'title="{esc}: 1 of 2 filled"' in doc
        assert 'title="Only in the split: 1 of 1 still missing"' in doc
        assert "Empty bucket" not in doc

    def test_a_document_without_a_module_split_keeps_the_plain_bar(self) -> None:
        """A report that carries document buckets but no module split (one
        serialised before the split existed) is drawn as it always was — one
        coverage fill — rather than an invented partition."""
        from builder.state import MITReport
        from builder.writers.maturity_report import MIT_MODULE_STYLES, _render_mit_section

        known = next(iter(MIT_MODULE_STYLES))
        report = MITReport(
            module_scores={known: {"completed": 1, "total": 4}},
            overall_score=0.25,
            standard_scores={"oecd_gd211": {"completed": 1, "total": 4}},
        )
        doc = self._row(_render_mit_section(report), "OECD GD 211")
        assert '<i class="fill-cov" style="width:25%"></i>' in doc
        assert 'class="mod"' not in doc


class TestUnassessedMITIsNotRenderedAsZero:
    """The report never prints a coverage number it did not measure (#311).

    When MIT coverage cannot be scored at all — the checklist will not load, or
    the crate will not assemble — the assessor returns an *unassessed* report
    whose ``overall_score`` is 0.0 by construction. Printing that as "0%" (and
    saying "MIT coverage 0%" through the meter's ``aria-label``) is a claim about
    a crate nobody looked at, and reads exactly like a crate that genuinely covers
    nothing. Same "not assessed" state the profile tile already shows for an
    unevaluated SHACL severity tier (#446).
    """

    @staticmethod
    def _unscoreable_page(monkeypatch: pytest.MonkeyPatch) -> str:
        # Break the checklist load rather than hand-build an MITReport: this
        # drives the real entry point and proves the report ASKS the assessor,
        # instead of asserting a page we wrote back to ourselves.
        monkeypatch.setattr("builder.tools.mit_assessment.load_mit_yaml", lambda: None)
        return build_maturity_html(vhps_fixture_state("S-VHPS21"))

    def test_no_percentage_is_printed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = self._unscoreable_page(monkeypatch)
        assert "MIT coverage 0%" not in page
        # No MIT meter at all, so no aria-label asserting a coverage figure.
        assert 'aria-label="MIT coverage' not in page

    def test_it_says_not_assessed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _body(self._unscoreable_page(monkeypatch))
        # The KPI tile's own sub-line, not the generic "na" mark that the
        # profile tile also carries — this must be the MIT tile saying it.
        assert '<div class="kpi-sub">not assessed</div>' in body
        # And the section says why, in words, rather than leaving a blank chart.
        assert "was not measured for this crate" in body
        # The old empty-scores section header read "0/0 fields · 0%", which
        # asserts a coverage figure just as loudly as the tile did.
        assert "fields · 0%" not in body

    def test_the_rest_of_the_report_still_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The assessor must not raise: three other axes depend on this page."""
        body = _body(self._unscoreable_page(monkeypatch))
        assert "FAIR" in body
        assert "Reproducibility readiness" in body
        assert "Profile adherence" in body

    def test_a_scoreable_crate_still_prints_its_number(self) -> None:
        """Honesty control: the "not assessed" state is reached by failure only.

        Without it, a bug that made every crate unscoreable would satisfy the
        three assertions above.
        """
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        assert _mit_pct(page) > 0
        assert 'aria-label="MIT coverage' in page


class TestDatasetsPanel:
    """The Datasets view: one row per data-category entity — kind, format,
    size, described, reachable — unreachable rows first."""

    def _graph(self) -> dict:
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "name": "T", "hasPart": [{"@id": "a.csv"}]},
                {"@id": "a.csv", "@type": "File", "name": "measurements_raw.csv",
                 "encodingFormat": "text/csv", "contentSize": "2048",
                 "description": "Raw endpoint readout"},
                {"@id": "loose.csv", "@type": "File", "name": "loose.csv"},
            ]
        }

    def test_rows_report_the_facts_and_unreachable_sorts_first(self) -> None:
        page = build_maturity_html(CrateState(), graph=self._graph())
        panel = page.split('id="p-data"', 1)[1].split("</div>\n", 1)[0]
        assert "Datasets" in page
        rows = re.findall(r"<tr><th scope=\"row\">.*?</tr>", panel, re.S)
        assert len(rows) == 2
        assert "loose.csv" in rows[0] and 'class="mk no"' in rows[0]
        assert "measurements_raw.csv" in rows[1]
        assert "text/csv" in rows[1] and "2 KB" in rows[1]
        assert "raw data" in rows[1]
        assert "1 of 2 data entities cannot be reached" in panel

    def test_no_data_entities_drops_the_tab(self) -> None:
        graph = {"@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": "T"},
        ]}
        page = build_maturity_html(CrateState(), graph=graph)
        assert 'id="p-data"' not in page.split("</style>", 1)[-1]


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

    @staticmethod
    def _compound_row(page: str) -> str:
        """The identification-matrix row for Aflatoxin B1 (the ``chem-tbl``
        class is shared with the ISA table, so look inside the chemicals panel)."""
        panel = page.split('id="p-chem"', 1)[1].split("</section>", 1)[0]
        table = re.search(r'<table class="chem-tbl">.*?</table>', panel, re.S)
        assert table, "no identification matrix"
        rows = [r for r in re.findall(r"<tr>.*?</tr>", table.group(0), re.S) if "Aflatoxin B1" in r]
        assert len(rows) == 1, "expected exactly one matrix row for the compound"
        return rows[0]

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

    def test_legend_explains_only_the_shapes_the_diagram_draws(self) -> None:
        """#506: the diagram draws compounds only, so the legend must too.

        A legend explaining a Process shape, a File shape and a "links to" edge
        that no longer appear teaches the reader to distrust the legend — the
        same rule the AUTOGENERATED swatch already follows.
        """
        panel = self._page(wire=False).split('id="p-chem"', 1)[1].split("</section>", 1)[0]
        legend = panel.split('<div class="prov-legend">', 1)[1].split("</div>", 1)[0]
        assert "Compound" in legend
        assert "not reachable" in legend
        for absent in ("Process", "File / table", "links to", "link missing"):
            assert absent not in legend, f"legend still explains {absent!r}"
        # Both keys are the diagram's real outline (#488's registry), and the
        # unreachable key carries the diagram's own `unwired` class — a
        # hand-drawn dashed swatch is exactly how a key drifts from its node.
        assert legend.count('class="n n-chemical"') == 1
        assert legend.count('class="n n-chemical unwired"') == 1

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
        # well described and still unreachable; both must be reported. The
        # identification lives in the matrix row (the KPI tile and the caption
        # are gone, #606): five ✓ against two ✗.
        cells = [
            m.group(1)
            for c in re.findall(r"<td>(.*?)</td>", self._compound_row(page), re.S)
            if (m := re.search(r'class="mk (ok|no)"', c))
        ]
        assert cells.count("ok") == 5 and cells.count("no") == 2
        assert "The substances under test" not in page

    def test_identifier_cells_link_to_their_external_source(self) -> None:
        """Every registry identifier a compound carries is a link to where a
        reader can look it up (CAS Common Chemistry, PubChem, the CompTox
        dashboard; an InChIKey searches PubChem). A structure field with no
        public resolver stays a plain mark, and a missing field stays ✗."""
        graph = self._graph()
        compound = next(n for n in graph["@graph"] if n["@id"] == "#compound")
        compound["identifier"] = [{"@id": "#cas"}, {"@id": "#cid"}, {"@id": "#dtx"}]
        graph["@graph"] += [
            {"@id": "#cid", "@type": "PropertyValue", "name": "PubChem CID", "value": "186907"},
            {"@id": "#dtx", "@type": "PropertyValue", "name": "DTXSID", "value": "DTXSID9020035"},
        ]
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        row = self._compound_row(page)
        cells = re.findall(r"<td>(.*?)</td>", row, re.S)
        assert len(cells) == 7  # CAS, CID, DTXSID, InChIKey, SMILES, Formula, Mass
        hrefs = [re.search(r'href="([^"]+)"', c) for c in cells]
        assert [h.group(1) if h else None for h in hrefs] == [
            "https://commonchemistry.cas.org/detail?cas_rn=1162-65-8",
            "https://pubchem.ncbi.nlm.nih.gov/compound/186907",
            "https://comptox.epa.gov/dashboard/chemical/details/DTXSID9020035",
            "https://pubchem.ncbi.nlm.nih.gov/#query=OQIQSTLJSLGHID-WNWIJWBNSA-N",
            None,  # SMILES
            None,  # Formula
            None,  # Mass
        ]
        for cell in cells[:4]:
            assert 'class="ext"' in cell and 'class="mk ok"' in cell, cell
        assert all('class="mk ok"' in c for c in cells[4:])
        # A missing identifier is a plain ✗, never a link.
        plain_cells = re.findall(r"<td>(.*?)</td>", self._compound_row(self._page()), re.S)
        assert 'class="mk no"' in plain_cells[1] and "href" not in plain_cells[1]  # no CID

    def test_resolvable_compound_name_links_to_its_identity(self) -> None:
        """A compound whose ``@id`` is an http(s) URL links its name there —
        the glyph that used to mark "resolvable" is now the link itself. Any
        other scheme is not a link: an ``@id`` is crate-controlled text and
        ``javascript:`` would otherwise reach the page as an href."""
        graph = self._graph()
        for node in graph["@graph"]:
            if node["@id"] == "#compound":
                node["@id"] = "https://pubchem.ncbi.nlm.nih.gov/compound/186907"
            if node["@id"] == "#table":
                node["about"] = [{"@id": "https://pubchem.ncbi.nlm.nih.gov/compound/186907"}]
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert (
            '<a class="ext" href="https://pubchem.ncbi.nlm.nih.gov/compound/186907">'
            "Aflatoxin B1</a>"
        ) in page
        assert "🔗" not in page.split('id="p-chem"', 1)[1].split("</section>", 1)[0]

        for node in graph["@graph"]:
            if node["@id"] == "https://pubchem.ncbi.nlm.nih.gov/compound/186907":
                node["@id"] = "javascript://alert(1)"
            if node["@id"] == "#table":
                node["about"] = [{"@id": "javascript://alert(1)"}]
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert 'href="javascript:' not in page

    def test_identifier_values_are_url_encoded_in_the_link(self) -> None:
        """An identifier value is crate text; it is percent-encoded into the URL
        and escaped into the attribute, so it can neither break out of the href
        nor smuggle markup."""
        graph = self._graph()
        for node in graph["@graph"]:
            if node["@id"] == "#cas":
                node["value"] = '1162-65-8"><script>x</script>'
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "<script>x</script>" not in page
        assert "cas_rn=1162-65-8%22%3E%3Cscript%3Ex%3C%2Fscript%3E" in page

    def test_the_kpi_grid_has_no_chemicals_tile(self) -> None:
        """#607 design handoff: the chemicals KPI tile is removed — the Chemicals
        graph view carries the wiring and identification facts."""
        page = self._page(wire=False)
        grid = re.search(r'<div class="kgrid">.*?</div>\n', page, re.S)
        assert grid, "no KPI grid"
        assert "Chemicals" not in grid.group(0)

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
                    "executesLabProtocol": {"@id": "#protocol"},
                },
                {
                    "@id": "#protocol",
                    "@type": "LabProtocol",
                    "name": "Exposure protocol",
                    "url": "https://www.protocols.io/view/exposure-abc",
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
                {
                    "@id": "https://doi.org/10.1007/s00204-024-03787-2",
                    "@type": "ScholarlyArticle",
                    "name": "Two novel in vitro assays for OATP1C1",
                    "datePublished": "2024",
                    "author": [{"@id": "https://orcid.org/0000-0002-1825-0097"}],
                },
            ]
        }

    def test_the_view_is_named_labprocesses_everywhere_it_shows(self) -> None:
        """#607 (owner's call): the derivation view is "LabProcesses" — in the
        tab, in the print heading, and in the diagram's accessible name. A
        label that reads one way on screen and another to a screen reader is
        two names for one view."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        body = _body(page)
        assert '<span class="tb-n">LabProcesses</span>' in body
        assert '<h3 class="panel-h">LabProcesses</h3>' in body
        assert 'aria-label="LabProcesses derivation chain"' in body
        assert "<title>LabProcesses derivation chain</title>" in body
        assert "Provenance" not in body

    def test_every_view_is_tabbed(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert "<h2>Graph views</h2>" in page
        for label in (
            "All entities",
            "Assays",
            "Datasets",
            "LabProcesses",
            "Chemicals",
            "Biological models",
            "Persons &amp; Organisations",
            "Citations",
        ):
            assert f'<span class="tb-n">{label}</span>' in page, f"missing tab: {label}"
        for pid in ("p-all", "p-isa", "p-prov", "p-chem", "p-cell", "p-people", "p-cite"):
            assert f'<div class="panel" id="{pid}">' in page, f"missing panel: {pid}"
        # The owner removed the LabProtocols tab: even a crate that carries a
        # protocol (this fixture does) renders no such view. Protocols still
        # appear as entities on the All-entities map.
        assert "LabProtocols" not in page
        assert 'id="p-lprot"' not in page and 'for="mv-lprot"' not in page

    def test_the_section_header_carries_no_view_count(self) -> None:
        """Review comment on the report artifact: the "N views of the same
        crate" meta said nothing a reader can act on — the tabs themselves
        are the count. Removed, not reworded."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert '<div class="sec-h"><h2>Graph views</h2></div>' in page
        assert "of the same crate" not in page

    def test_no_topology_strip_or_detail(self) -> None:
        """Review comment: the graph-topology block (strip + expandable
        detail) is gone from the Graph views section."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert "Graph topology" not in page
        assert "topo-label" not in page

    def test_the_review_tab_order_holds(self) -> None:
        """Review comment ("flip dataset and assays"): Datasets sits before
        Assays in the tab bar."""
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph()))
        assert body.index('for="mv-data"') < body.index('for="mv-isa"')

    def test_the_isa_and_people_views_wear_their_review_names(self) -> None:
        """Owner's calls, left as review comments on the report artifact: the
        ISA backbone tab reads "Assays" and the credit tab "Persons &
        Organisations". The ISA diagram's accessible name follows its tab
        (the LabProcesses precedent); the old names appear nowhere."""
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph()))
        assert '<span class="tb-n">Assays</span>' in body
        assert '<span class="tb-n">Persons &amp; Organisations</span>' in body
        assert "Assays: " in body, "the ISA diagram's accessible name kept the old view name"
        assert "ISA structure" not in body
        assert "People &amp; orgs" not in body

    def test_inputs_precede_the_tabbar_and_panels(self) -> None:
        # The CSS-only mechanism is `input:checked ~ .panel`; a panel emitted
        # before its radio can never be revealed.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        last_input = page.rindex('<input class="tab-in"')
        assert last_input < page.index('<div class="tabbar">')
        assert last_input < page.index('<div class="panel"')

    def test_exactly_one_tab_starts_selected(self) -> None:
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph()))
        assert body.count('name="mat-view"') == 8  # the eight live views
        assert body.count(" checked>") == 1
        # ISA is first: the structural backbone every other view hangs off.
        assert 'id="mv-all" checked>' in body

    def test_tabs_carry_no_script(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert "<script" not in page.lower()
        assert "onclick" not in page.lower()

    def test_absent_views_drop_their_tab_and_first_survivor_is_selected(self) -> None:
        # No compounds, no cell lines, nobody credited and nothing cited. The
        # root Dataset is still an Investigation, so ISA survives alongside
        # Provenance — and the first surviving tab must be the selected one,
        # never a dead tab bar.
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
        for absent in (
            "mv-chem", "p-chem", "mv-cell", "p-cell",
            "mv-people", "p-people", "mv-cite", "p-cite",
        ):
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

    def test_every_registered_view_is_styled(self) -> None:
        # The tabs are pure CSS, and the stylesheet names each id by hand. A view
        # added to `_VIEWS` without its four selectors renders a tab that cannot
        # be selected and a panel that never shows — with no error anywhere.
        from builder.writers.maturity_report import _VIEWS, _load_css

        css = _load_css()
        for rid, pid, _label in _VIEWS:
            for selector in (
                f'#{rid}:checked ~ .tabbar .tab[for="{rid}"]',
                f'#{rid}:checked ~ .tabbar .tab[for="{rid}"] .tb-c',
                f'#{rid}:focus-visible ~ .tabbar .tab[for="{rid}"]',
                f"#{rid}:checked ~ #{pid}",
            ):
                assert selector in css, f"unstyled view selector: {selector}"

    def test_print_styles_expand_every_panel(self) -> None:
        # Tabs are a screen affordance; a printed report must not silently lose
        # two of the three views.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert ".mat .panel{display:block !important;" in page.replace("\n", "")
        assert ".mat .panel > .panel-h{display:block;" in page.replace("\n", "")

class TestCellLinesPanel:
    """The Biological models view (#85): the biological test system, and
    whether it is pinned down.

    A model fails the same two ways a compound does — unreachable when the
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

    @staticmethod
    def _compound_row(page: str) -> str:
        """The identification-matrix row for Aflatoxin B1 (the ``chem-tbl``
        class is shared with the ISA table, so look inside the chemicals panel)."""
        panel = page.split('id="p-chem"', 1)[1].split("</section>", 1)[0]
        table = re.search(r'<table class="chem-tbl">.*?</table>', panel, re.S)
        assert table, "no identification matrix"
        rows = [r for r in re.findall(r"<tr>.*?</tr>", table.group(0), re.S) if "Aflatoxin B1" in r]
        assert len(rows) == 1, "expected exactly one matrix row for the compound"
        return rows[0]

    def test_renders_diagram_and_matrix(self) -> None:
        page = self._page()
        assert '<div class="panel" id="p-cell">' in page
        assert '<span class="tb-n">Biological models</span>' in page
        assert "CHO-K1" in page
        assert "CVCL_0214" in page
        for column in ("RRID", "Type", "Organ", "Tissue", "Passage"):
            assert f">{column}</th>" in page, f"missing coverage column: {column}"

    def test_the_view_is_named_biological_models_wherever_a_reader_sees_it(self) -> None:
        """The owner's term for the test system is the checklist's — "biological
        model", as in the MIT module Biological Model Information — so the tab,
        the table's corner header, the summary line, the legend and the
        diagram's accessible name all say it. "Cell line" survives only where
        it names the actual declaration being checked: the Type column's
        tooltip (the entity is typed as a cell line) and ``CellLine`` itself.
        """
        page = self._page()
        assert '<span class="tb-n">Biological models</span>' in page
        assert '<th scope="col">Biological model</th>' in page
        assert "<b>1</b> biological model ·" in page
        assert "Biological model / sample</span>" in page
        assert "Routes: 1 of 1 biological models reachable from a process" in page
        panel = page.split('id="p-cell"', 1)[1].split("</section>", 1)[0]
        rest = panel.replace('title="Typed as a cell line"', "")
        assert "cell line" not in rest.lower(), "a reader-facing 'cell line' survived"

    def test_unconsumed_model_is_called_out_with_the_fix(self) -> None:
        page = self._page(wire=False)
        assert "1 of 1 biological models are not consumed by any process." in page
        assert "<code>CellCulture</code>" in page
        assert "<code>input</code>" in page

    def test_consumed_model_reports_a_clean_route(self) -> None:
        page = self._page(wire=True, rrid=True)
        assert "not consumed by any process" not in page

    def test_missing_rrid_is_called_out_separately_from_wiring(self) -> None:
        # Correctly consumed but unidentified: the two defects are independent
        # and collapsing them would hide whichever the crate actually has.
        page = self._page(wire=True, rrid=False)
        assert "not consumed by any process" not in page
        assert "1 of 1 biological models carry no Cellosaurus RRID." in page

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

    @staticmethod
    def _compound_row(page: str) -> str:
        """The identification-matrix row for Aflatoxin B1 (the ``chem-tbl``
        class is shared with the ISA table, so look inside the chemicals panel)."""
        panel = page.split('id="p-chem"', 1)[1].split("</section>", 1)[0]
        table = re.search(r'<table class="chem-tbl">.*?</table>', panel, re.S)
        assert table, "no identification matrix"
        rows = [r for r in re.findall(r"<tr>.*?</tr>", table.group(0), re.S) if "Aflatoxin B1" in r]
        assert len(rows) == 1, "expected exactly one matrix row for the compound"
        return rows[0]

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


class TestCitationsPanel:
    """The Citations view (#85): the literature the crate stands on.

    A citation refers to two things at once, and each can fail on its own. The
    work needs a DOI and something in the crate has to point at it; the credit
    list has to reach entities the crate contains. The second is a shipping
    defect — a Crossref author with no ORCID is minted as ``#CitationAuthor_…``
    and nothing in the ``@graph`` answers to that ``@id`` (#532), leaving an
    article that looks fully attributed in the JSON and credits nobody.
    """

    def _graph(self, *, cite: bool = True, doi: bool = True, dangling: bool = False) -> dict:
        article_id = "https://doi.org/10.1007/s00204-024-03787-2" if doi else "#Publication_oatp"
        root: dict = {"@id": "./", "@type": "Dataset", "name": "Crate"}
        if cite:
            root["citation"] = [{"@id": article_id}]
        authors: list[dict] = [{"@id": "https://orcid.org/0000-0002-1825-0097"}]
        if dangling:
            authors.append({"@id": "#CitationAuthor_Zhongli_Chen"})
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                root,
                {
                    "@id": article_id,
                    "@type": "ScholarlyArticle",
                    "name": "Two novel in vitro assays for OATP1C1",
                    "datePublished": "2024",
                    "author": authors,
                },
                {
                    "@id": "https://orcid.org/0000-0002-1825-0097",
                    "@type": "Person",
                    "name": "Josiah Carberry",
                },
            ]
        }

    def _page(self, **kw: bool) -> str:
        return build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph(**kw))

    @staticmethod
    def _compound_row(page: str) -> str:
        """The identification-matrix row for Aflatoxin B1 (the ``chem-tbl``
        class is shared with the ISA table, so look inside the chemicals panel)."""
        panel = page.split('id="p-chem"', 1)[1].split("</section>", 1)[0]
        table = re.search(r'<table class="chem-tbl">.*?</table>', panel, re.S)
        assert table, "no identification matrix"
        rows = [r for r in re.findall(r"<tr>.*?</tr>", table.group(0), re.S) if "Aflatoxin B1" in r]
        assert len(rows) == 1, "expected exactly one matrix row for the compound"
        return rows[0]

    def test_renders_diagram_and_matrix(self) -> None:
        page = self._page()
        assert '<div class="panel" id="p-cite">' in page
        assert '<span class="tb-n">Citations</span>' in page
        assert "Two novel in vitro assays for OATP1C1" in page
        assert "10.1007/s00204-024-03787-2" in page
        for column in ("DOI", "Title", "Date", "Authors", "Resolve", "Cited"):
            assert f">{column}</th>" in page, f"missing citation column: {column}"

    def test_uncited_article_is_called_out_with_the_fix(self) -> None:
        page = self._page(cite=False)
        assert "1 of 1 articles are cited by nothing in the crate" in page
        assert "<code>citation</code>" in page

    def test_missing_doi_is_called_out_separately_from_the_route(self) -> None:
        # Correctly cited but unretrievable: the two defects are independent, and
        # collapsing them would hide whichever the crate actually has.
        page = self._page(doi=False)
        assert "cited by nothing in the crate" not in page
        assert "1 of 1 articles carry no resolvable DOI." in page

    def test_author_that_resolves_to_nothing_is_reported(self) -> None:
        page = self._page(dangling=True)
        assert "1 author reference resolves to no entity in the crate." in page
        assert "#CitationAuthor_&hellip;" in page
        assert "1 of 2 authors unresolved" in page

    def test_clean_citation_reports_no_defect(self) -> None:
        page = self._page()
        assert "Every article is cited, DOI-backed, and every author resolves." in page
        assert "resolve to no entity" not in page

    def test_legend_names_the_dashed_author_only_when_one_is_drawn(self) -> None:
        # A key for a shape the reader cannot find teaches them to distrust the
        # legend — and here it would also misdescribe the crate.
        assert "Author reference that resolves to nothing" in self._page(dangling=True)
        assert "Author reference that resolves to nothing" not in self._page()

    def test_crate_without_articles_omits_the_view(self) -> None:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#f"}]},
                {"@id": "#f", "@type": "File", "name": "result.csv"},
            ]
        }
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph))
        assert 'id="p-cite"' not in body
        assert 'for="mv-cite"' not in body

    def test_escapes_article_names(self) -> None:
        graph = self._graph()
        graph["@graph"][2]["name"] = "<script>alert(1)</script>"
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_escapes_a_crate_controlled_doi(self) -> None:
        # The DOI reaches the matrix as a chip of its own, outside the `label`
        # the inventory pre-escapes — a second path from crate text to the page.
        # A DOI suffix is opaque and may legally carry `&`, so the chip cannot
        # rely on the pattern alone to keep markup out.
        graph = self._graph(doi=False)
        graph["@graph"][2]["identifier"] = "10.1000/a&b<script>alert(1)</script>"
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "<script>alert(1)</script>" not in page
        assert "10.1000/a&amp;b" in page


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


def _visible_before_the_fold(html: str) -> str:
    """The part of a findings list a reader sees without opening the overflow.

    The cap bounds what is shown, not what exists: the remainder now sits in a
    nested `<details class="more-fold">`. Counting occurrences across the whole
    string used to measure the cap and no longer does, because the hidden items
    are in the markup too — closed, but present.
    """
    # Strip each fold whole, keeping what surrounds it. Two earlier attempts got
    # this wrong in opposite directions: a non-greedy match to `</li>` ends INSIDE
    # the fold (it contains <li> items) and leaks the remainder; splitting on the
    # opening tag drops every later profile group, which is exactly what the cap
    # is meant to be measured across. `</details></li>` closes it unambiguously.
    return re.sub(r'<li class="more">.*?</details></li>', "", html, flags=re.S)


class TestGroupedSuggestions:
    """Warnings fold out of the severity row they belong to (#510).

    Severity is the primary axis because it is the fix order: REQUIRED blocks
    the build, the advisory tiers do not. So each "By severity" row carries its
    own findings, grouped by profile layer inside the fold (base → ISA →
    ISA-Tox, the gate-ordering contract) — one index, not a tier index plus a
    profile index restating the same counts. A row holding REQUIRED findings is
    born open (a collapsed fold must never hide a blocking issue); advisory rows
    start collapsed; a row with nothing to show does not fold at all. A verdict
    from an older checkpoint (no records) keeps the flat list.
    """

    @staticmethod
    def _fold(body: str, tier: str) -> str:
        """The ``<details>`` markup for one severity row.

        Balances the tags rather than slicing to the first ``</details>``: the
        advisory overflow is itself a ``<details>`` nested inside this one, so the
        first closer belongs to the inner fold and cutting there truncated the
        section — dropping every later profile group with it.
        """
        start = body.index(f'<span class="st">{tier}</span>')
        open_at = body.rindex("<details", 0, start)
        depth, i = 0, open_at
        while i < len(body):
            nxt_open = body.find("<details", i)
            nxt_close = body.find("</details>", i)
            if nxt_close == -1:
                break
            if nxt_open != -1 and nxt_open < nxt_close:
                depth += 1
                i = nxt_open + len("<details")
                continue
            depth -= 1
            if depth == 0:
                return body[open_at:nxt_close]
            i = nxt_close + len("</details>")
        return body[open_at:]

    @staticmethod
    def _records_report() -> ValidationReport:
        return ValidationReport(
            base_passed=False,
            isa_passed=True,
            tox_passed=True,
            required_issues=["[base] ./: root MUST have a name"],
            should_issues=[
                "[isa] #study-1: consider adding a license",
                "[tox] #assay-1: dose units are recommended",
            ],
            may_issues=["[isa] #study-1: a DOI would help"],
            assessed_tiers={"required", "recommended", "optional"},
            issue_records=[
                {
                    "profile": "base",
                    "severity": "required",
                    "entity_id": "./",
                    "message": "root MUST have a name",
                },
                {
                    "profile": "isa",
                    "severity": "recommended",
                    "entity_id": "#study-1",
                    "message": "consider adding a license",
                },
                {
                    "profile": "tox",
                    "severity": "recommended",
                    "entity_id": "#assay-1",
                    "message": "dose units are recommended",
                },
                {
                    "profile": "isa",
                    "severity": "optional",
                    "entity_id": "#study-1",
                    "message": "a DOI would help",
                },
            ],
        )

    def test_findings_fold_out_of_their_own_severity_row(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        body = _body(build_maturity_html(state, validation=self._records_report()))
        rec = self._fold(body, "Recommended")
        # The row keeps its severity summary…
        assert "2 issues" in rec
        # …and carries its findings, grouped by profile in fix order.
        assert rec.index('>ISA<span class="pc">') < rec.index('>ISA-Tox<span class="pc">')
        assert "consider adding a license" in rec
        assert "dose units are recommended" in rec
        # Findings of another tier belong to another row.
        assert "a DOI would help" not in rec
        assert "a DOI would help" in self._fold(body, "Optional")

    def test_profile_counts_are_not_restated_outside_the_fold(self) -> None:
        # The defect this replaces: a per-profile list under the severity block
        # restating the same counts ("Recommended · 2 issues", then "ISA — 1
        # recommended · ISA-Tox — 1 recommended").
        state = vhps_fixture_state("S-VHPS21")
        body = _body(build_maturity_html(state, validation=self._records_report()))
        assert "sugg-prof" not in body.replace('class="sugg-prof-h"', "")
        assert "1 recommended · 1 optional" not in body

    def test_row_with_required_findings_is_open_advisory_rows_collapsed(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        body = _body(build_maturity_html(state, validation=self._records_report()))
        assert self._fold(body, "Required").startswith('<details class="sev-fold" open>')
        assert self._fold(body, "Recommended").startswith('<details class="sev-fold">')
        assert self._fold(body, "Optional").startswith('<details class="sev-fold">')

    def test_rows_with_nothing_to_show_do_not_fold(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            assessed_tiers={"required"},
            issue_records=[],
        )
        body = _body(build_maturity_html(state, validation=val))
        # A clean REQUIRED tier and two unassessed tiers: nothing to unfold, and
        # the unassessed ones still say so rather than reading as clean (#306).
        assert "sev-fold" not in body
        assert "not assessed" in body.lower()

    def test_items_show_entity_and_message_without_the_string_prefix(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        body = _body(build_maturity_html(state, validation=self._records_report()))
        assert "<code>./</code>" in body
        assert "root MUST have a name" in body
        assert "Must fix" in body
        assert "Recommended:" in body and "Optional:" in body
        # The grouped view replaces the flattened "[profile] entity:" prefix.
        assert "[isa]" not in body and "[base]" not in body

    def test_advisory_caps_apply_per_profile_group(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        records = [
            {
                "profile": "isa",
                "severity": "recommended",
                "entity_id": f"#e{i}",
                "message": f"advisory finding {i}",
            }
            for i in range(12)
        ]
        val = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            should_issues=[f"[isa] #e{i}: advisory finding {i}" for i in range(12)],
            assessed_tiers={"required", "recommended", "optional"},
            issue_records=records,
        )
        body = _body(build_maturity_html(state, validation=val))
        assert _visible_before_the_fold(body).count("Recommended: ") == 10
        assert "+2 further recommended findings" in body

    def test_records_are_escaped(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=False,
            isa_passed=True,
            tox_passed=True,
            required_issues=["[base] #x: <img src=x onerror=alert(1)>"],
            assessed_tiers={"required"},
            issue_records=[
                {
                    "profile": "base",
                    "severity": "required",
                    "entity_id": "#<b>x</b>",
                    "message": "<img src=x onerror=alert(1)>",
                }
            ],
        )
        page = build_maturity_html(state, validation=val)
        assert "<img src=x" not in page
        assert "<b>x</b>" not in page
        assert "&lt;img src=x" in page

    def test_verdict_without_records_folds_its_display_strings(self) -> None:
        # A verdict recorded before issue_records existed carries only the flat
        # strings. They still belong to a tier, so they still fold out of it —
        # ungrouped, because such a verdict has no profile attribution to group by.
        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=False,
            isa_passed=True,
            tox_passed=True,
            required_issues=["root MUST have a name"],
            should_issues=["consider adding a license"],
        )
        body = _body(build_maturity_html(state, validation=val))
        assert "root MUST have a name" in self._fold(body, "Required")
        assert "consider adding a license" in self._fold(body, "Recommended")

    def test_a_tier_whose_records_are_missing_still_shows_its_findings(self) -> None:
        """The mixed state: records for one tier, display strings for another.

        A pre-records checkpoint that then takes a REQUIRED-gate write-back ends
        up with required records beside still-fresh advisory strings. Choosing
        the rendering once, for the whole report, hid those advisory findings
        while the severity row went on counting them — the report would count
        findings its own list omits.
        """
        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=False,
            isa_passed=True,
            tox_passed=True,
            required_issues=["[base] ./: root MUST have a name"],
            should_issues=["[isa] #s: consider a license", "[tox] #a: add dose units"],
            may_issues=["[isa] #s: a DOI would help"],
            assessed_tiers={"required", "recommended", "optional"},
            issue_records=[
                {
                    "profile": "base",
                    "severity": "required",
                    "entity_id": "./",
                    "message": "root MUST have a name",
                }
            ],
        )
        body = _body(build_maturity_html(state, validation=val))
        rec = self._fold(body, "Recommended")
        assert "2 issues" in rec
        assert "consider a license" in rec and "add dose units" in rec
        assert "a DOI would help" in self._fold(body, "Optional")

    def test_cap_applies_per_profile_group_not_across_the_tier(self) -> None:
        # Two layers of 12 findings each: the cap bounds each group at 10, so
        # both groups keep a listed head and both name what they hid — a global
        # cap would have shown 10 of 24 and hidden a whole layer.
        state = vhps_fixture_state("S-VHPS21")
        records = [
            {
                "profile": profile,
                "severity": "recommended",
                "entity_id": f"#{profile}{i}",
                "message": f"{profile} advisory {i}",
            }
            for profile in ("isa", "tox")
            for i in range(12)
        ]
        val = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            should_issues=[f"{r['profile']} advisory {r['entity_id']}" for r in records],
            assessed_tiers={"required", "recommended", "optional"},
            issue_records=records,
        )
        rec = self._fold(_body(build_maturity_html(state, validation=val)), "Recommended")
        assert _visible_before_the_fold(rec).count("Recommended: ") == 20
        assert rec.count("+2 further recommended findings") == 2
        assert "24 issues" in rec

    def test_unattributed_findings_are_reported_not_dropped(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            should_issues=["one with no layer"],
            assessed_tiers={"required", "recommended", "optional"},
            issue_records=[
                {
                    "profile": "",
                    "severity": "recommended",
                    "entity_id": "#x",
                    "message": "one with no layer",
                }
            ],
        )
        rec = self._fold(_body(build_maturity_html(state, validation=val)), "Recommended")
        assert "unattributed" in rec
        assert "one with no layer" in rec

    def test_a_severity_outside_the_three_tiers_grows_its_own_row(self) -> None:
        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            assessed_tiers={"required", "recommended", "optional"},
            issue_records=[
                {
                    "profile": "base",
                    "severity": "info",
                    "entity_id": "#x",
                    "message": "an informational note",
                }
            ],
        )
        body = _body(build_maturity_html(state, validation=val))
        assert "an informational note" in self._fold(body, "Info")

    def test_a_findings_row_reports_the_count_it_unfolds(self) -> None:
        # The row's count and its contents come from one list, so the summary
        # can never promise a number the fold does not hold.
        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            should_issues=["[isa] #a: one"],
            assessed_tiers={"required", "recommended", "optional"},
            issue_records=[
                {"profile": "isa", "severity": "recommended", "entity_id": "#a", "message": "one"},
                {"profile": "tox", "severity": "recommended", "entity_id": "#b", "message": "two"},
            ],
        )
        body = _body(build_maturity_html(state, validation=val))
        rec = self._fold(body, "Recommended")
        assert "2 issues" in rec
        assert rec.count("Recommended: ") == 2


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
        assert "unlinked entity</span>" in page

    def test_clean_crate_shows_no_unreachable_state(self) -> None:
        # With the prose note gone (owner's review), a clean crate simply has
        # no orphan-outlined tile and no reachability key to explain one.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph(orphan=False))
        assert "orphan" not in page.split('id="p-all"', 1)[1].split('<div class="panel"', 1)[0]
        assert "unlinked entity" not in page

    def test_every_tile_names_its_entity(self) -> None:
        # The map summarises; it must not anonymise. Each tile carries the
        # entity's name and type in a tooltip.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        assert "<title>Unwired compound — MolecularEntity" in page
        assert "linked to nothing at all</title>" in page

    def test_bands_and_clusters_name_the_layers_and_the_types(self) -> None:
        """Review comments on the report artifact: the band headings are the
        plain layer names — RO-Crate, ISA RO-Crate, ISA-Tox RO-Crate — and
        each cluster caption names the entity types it actually holds, not a
        category word. The caption is what the tiles are: "Term / parameter"
        over three CreativeWorks made the reader guess, and guess wrong."""
        import re

        graph = self._graph()
        # Two packaging-layer annotation entities of different types, so one
        # cluster caption must name both types.
        graph["@graph"] += [
            {"@id": "#tool", "@type": "SoftwareApplication", "name": "vitro-crate"},
            {"@id": "#doc", "@type": "CreativeWork", "name": "Readme"},
        ]
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph))
        svg = re.search(r'<svg [^>]*class="prov view overview".*?</svg>', body, re.S)
        assert svg, "overview SVG missing"
        svg_text = svg.group(0)
        # The band headings are the bare layer names — no entity counts
        # (review: "remove" on every "— N entities" suffix). The counts still
        # ride on the diagram's accessible name and the note below the map.
        assert ">RO-Crate</text>" in svg_text
        assert ">ISA-Tox RO-Crate</text>" in svg_text
        bands = re.findall(r'class="ov-band"[^>]*>([^<]*)</text>', svg_text)
        assert bands and all("entit" not in band for band in bands), bands
        for old in ("Packaging", "Structural", "Domain"):
            assert old not in svg_text, f"the old layer taxonomy survived: {old}"
        assert ">Dataset · 1</text>" in svg_text
        assert ">MolecularEntity · 1</text>" in svg_text
        assert ">File · 1</text>" in svg_text
        assert ">CreativeWork / SoftwareApplication · 2</text>" in svg_text
        for word in ("Term / parameter", "Sample / material", "File / table",
                     "Investigation / Study / Assay"):
            assert word not in svg_text, f"a category word survived as a caption: {word}"

    def test_the_overview_carries_no_intro_paragraph(self) -> None:
        """Review comment: the "Every entity in the crate, one tile each…"
        summary line is gone — the map opens the panel directly. The
        unreachable story lives on the map itself: outlined tiles and the
        "unlinked entity" key."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        panel = page.split('id="p-all"', 1)[1].split('<div class="panel"', 1)[0]
        assert '<p class="prov-cap">' not in panel
        assert "one tile each" not in page

    def test_the_legend_carries_no_category_words(self) -> None:
        """Review comment: every word on this view should be a schema.org /
        bioschemas type. The cluster captions now name the types, so the
        category-word swatch list would either duplicate them or lie (one
        colour spans several types) — it is dropped. The reachability keys
        stay: solid/outlined is a state no caption names."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        panel = page.split('id="p-all"', 1)[1].split('<div class="panel"', 1)[0]
        assert 'ov-key cat-' not in panel, "the category swatch list survived"
        assert 'ov-key orphan isolated' in panel, "the reachability key was lost"

    def test_the_map_carries_no_warning_note(self) -> None:
        """Review comment: the "N of M entities are unreachable…" paragraph
        under the map is gone — the outlined tiles and the "unlinked entity"
        key carry that state on the map itself."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        panel = page.split('id="p-all"', 1)[1].split('<div class="panel"', 1)[0]
        assert "are unreachable from the crate root" not in panel
        assert "chem-warn" not in panel and "good-note" not in panel

    def test_the_isolated_key_reads_unlinked_entity(self) -> None:
        """Review comment: the isolated-orphan key says "unlinked entity"."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=self._graph())
        panel = page.split('id="p-all"', 1)[1].split('<div class="panel"', 1)[0]
        assert '<span class="ov-key orphan isolated"></span> unlinked entity</span>' in panel
        assert "unreachable · linked to nothing" not in panel

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


class TestOverviewReachabilityKeys:
    """The overview legend names only the unreachable kinds the map shows.

    The prose repair estimate this class once covered was removed with the
    warning note on the owner's review; the keys are what remains of the
    unreachable story off the tiles themselves.
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

    def test_the_legend_names_only_the_kinds_present(self) -> None:
        """A key for a tile the reader cannot find misdescribes the crate."""
        both = self._page()
        island_only = self._page(lone=0)

        assert "unlinked entity</span>" in both
        assert "linked to each other, not to the root" in both
        assert "unlinked entity</span>" not in island_only

class TestEveryClassTheReportEmitsIsStyled:
    """A class in the HTML with no rule in the CSS renders at browser defaults.

    "What to do next" shipped with four such classes. The section still LOOKED
    plausible in a diff — the markup is correct and the text is right — but the
    count and the sentence are two adjacent inline spans, so with no rule they
    ran together as "36Connect the AOP-Wiki subgraph". Nothing failed; the page
    was just wrong.

    Scoped to the classes the report defines for ITSELF. Bare element styling
    and any class inherited from elsewhere are not this test's business — the
    claim is only that a class this file invents has somewhere to get its layout
    from.
    """

    def _emitted_classes(self, page: str) -> set[str]:
        import re

        return {
            cls
            for attr in re.findall(r'class="([^"]+)"', page)
            for cls in attr.split()
        }

    def _next_steps_html(self) -> str:
        """The "What to do next" section, actually rendered.

        Built here rather than taken from a fixture page because NO fixture
        produces it: it needs a ValidationReport carrying `issue_records`, and
        every golden crate renders the page without one. That is how four
        unstyled classes shipped — and it is also why the first version of this
        test passed with the rules deleted. It searched the whole page for the
        class NAME, and the page INLINES the stylesheet, so every class matched
        itself in the `<style>` block whether or not the markup used it.
        """
        from builder.tools.validation import ValidationReport
        from builder.writers.maturity_report import (
            _render_recommendations,
            _render_references,
        )

        val = ValidationReport()
        val.base_passed = True
        val.issue_records = [
            {"profile": "base", "severity": "required", "entity_id": "./",
             "message": "The root Dataset MUST have a licence"},
            *[
                {"profile": "tox", "severity": "optional", "entity_id": f"#e{i}",
                 "message": f"A Sample SHOULD have a description of kind {i}"}
                for i in range(11)
            ],
        ]
        # Recommendations AND the references block its footnote links point at —
        # both carry classes of their own, so covering only the section would
        # leave exactly the gap this test exists to close.
        html_out = _render_recommendations(val, None) + _render_references()
        assert 'class="rec-n"' in html_out, "the section did not render; this test is inert"
        assert 'class="refs"' in html_out, "the references did not render; this test is inert"
        return html_out

    def test_no_class_in_the_page_is_missing_from_the_stylesheet(self) -> None:
        from pathlib import Path

        css = Path("builder/writers/maturity_report.css").read_text(encoding="utf-8")
        # The whole page PLUS the section no fixture renders. Concatenated so one
        # assertion covers both, and the section's classes cannot be quietly
        # dropped from coverage again.
        page = build_maturity_html(vhps_fixture_state("S-VHPS21")) + self._next_steps_html()

        # Classes the page carries but the stylesheet never mentions. `.mat` is
        # the wrapper the stylesheet keys everything off, and `mermaid` is the
        # renderer's own hook — neither is styled here by design.
        exempt = {"mat", "mermaid"}
        unstyled = sorted(
            cls
            for cls in self._emitted_classes(page)
            if cls not in exempt and f".{cls}" not in css
        )
        assert unstyled == [], (
            f"classes emitted with no rule in maturity_report.css, so they render "
            f"at browser defaults: {unstyled}"
        )


class TestRecommendationRows:
    """#607 design handoff: each row is the validator's own shape message in a mono
    chip prefixed by its source layer, the severity badge, then the bold
    plain-language instruction with one muted clause on why it matters."""

    def _section(self) -> str:
        from builder.tools.validation import ValidationReport
        from builder.writers.maturity_report import _render_recommendations

        val = ValidationReport()
        val.base_passed = True
        val.issue_records = [
            {"profile": "tox", "severity": "recommended",
             "entity_id": "./#CellLineSample_cell_h4",
             "message": "Entity SHOULD have a non-empty identifier"},
            {"profile": "tox", "severity": "recommended",
             "entity_id": "./#CellLineSample_cell_h4",
             "message": "Entity SHOULD have a non-empty description"},
            *[
                {"profile": "base", "severity": "recommended", "entity_id": f"./#File_f{i}",
                 "property": "creator", "message": "A File SHOULD have a creator"}
                for i in range(9)
            ],
        ]
        return _render_recommendations(val, None)

    def test_the_chip_quotes_the_validator_with_its_source(self) -> None:
        """The raw message survives the grouping verbatim, prefixed by the
        profile layer the record names — the reader sees the validator's own
        words, not only our paraphrase."""
        section = self._section()
        assert (
            '<code class="rec-chip">RO-Crate 1.2 &middot; '
            "A File SHOULD have a creator</code>" in section
        )
        assert (
            '<code class="rec-chip">ISA-Tox &middot; '
            "Entity SHOULD have a non-empty identifier</code>" in section
        )

    def test_every_row_carries_its_severity_badge(self) -> None:
        import re

        section = self._section()
        rows = re.findall(r"<li>.*?</li>", section, re.S)
        assert len(rows) == 2
        for row in rows:
            assert '<span class="rec-badge rec">Recommended</span>' in row

    def test_the_instruction_is_bold_with_a_why_clause(self) -> None:
        """`describe`'s sentence in the bold span; `why`'s clause muted after —
        matched over the action's own findings the way the instruction is
        (most-specific first: the H4 blob holds description AND identifier, and
        description wins in both tables), never a generic platitude."""
        section = self._section()
        assert '<span class="rec-do">Add a description for' in section
        assert (
            '<span class="rec-why">Nobody can tell what it is for without one.</span>' in section
        )

    def test_the_count_column_is_the_findings_cleared(self) -> None:
        section = self._section()
        assert '<span class="rec-n">9</span>' in section  # the nine File findings
        assert '<span class="rec-n">2</span>' in section  # the two H4 findings

    def test_a_required_row_wears_the_red_badge(self) -> None:
        from builder.tools.validation import ValidationReport
        from builder.writers.maturity_report import _render_recommendations

        val = ValidationReport()
        val.base_passed = False
        val.required_issues = ["[base] ./: The root Dataset MUST have a licence"]
        val.issue_records = [
            {"profile": "base", "severity": "required", "entity_id": "./",
             "message": "The root Dataset MUST have a licence"},
        ]
        section = _render_recommendations(val, None)
        assert '<span class="rec-badge req">Required</span>' in section
        assert "Add a reuse licence" in section
        assert "Nobody may legally reuse the data without one." in section

    def test_a_merged_row_keeps_the_validator_chip(self) -> None:
        """Entities folded into one "entities" action share a finding
        signature, so the merged row still quotes the validator — losing the
        chip on merge silently broke the section's own lead claim."""
        from builder.tools.validation import ValidationReport
        from builder.writers.maturity_report import _render_recommendations

        val = ValidationReport()
        val.base_passed = True
        val.issue_records = [
            {"profile": "isa", "severity": "recommended", "entity_id": eid, "message": m}
            for eid in ("#p1", "#p2")
            for m in (
                "Person SHOULD have an ORCID",
                "Person SHOULD have an affiliation",
            )
        ]
        section = _render_recommendations(val, None)
        assert (
            '<code class="rec-chip">ISA &middot; Person SHOULD have an ORCID</code>' in section
        )

    def test_the_provenance_note_claims_shacl_only_when_a_verdict_exists(self) -> None:
        """The crate-card note may say "conformance from a SHACL validation"
        only when a fresh verdict exists — a note claiming a validation nobody
        ran is the same false green the matrix refuses to show."""
        graph = {"@graph": [{"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                            {"@id": "./", "@type": "Dataset", "name": "T"}]}
        unvalidated = build_maturity_html(CrateState(), graph=graph)
        assert "This report is" in unvalidated
        assert "SHACL" not in unvalidated.split("</style>", 1)[-1]
        state = vhps_fixture_state("S-VHPS21")
        validated = build_maturity_html(
            state,
            validation=ValidationReport(
                base_passed=True, isa_passed=True, tox_passed=True,
                input_fingerprint=state.validation_fingerprint(),
            ),
            graph=graph,
        )
        assert "conformance from a SHACL validation against the three profiles" in validated

    def test_rows_are_ordered_tier_then_impact_then_size(self) -> None:
        """The renderer's own order, asserted through the renderer: a required
        action outranks any advisory one however small, and within a tier the
        more valuable fix outranks the bulkier one."""
        from builder.tools.validation import ValidationReport
        from builder.writers.maturity_report import _render_recommendations

        val = ValidationReport()
        val.base_passed = False
        val.required_issues = ["[base] ./: The root Dataset MUST have a licence"]
        val.issue_records = [
            {"profile": "base", "severity": "required", "entity_id": "./",
             "message": "The root Dataset MUST have a licence"},
            # Impact band 0 — the values cannot be interpreted without it.
            {"profile": "isa", "severity": "recommended", "entity_id": "#a",
             "message": "Assay SHOULD have a measurement technique"},
            {"profile": "isa", "severity": "recommended", "entity_id": "#a",
             "message": "Assay SHOULD name its measured entity"},
            # Impact band 1, but nine times the size.
            *[
                {"profile": "base", "severity": "recommended", "entity_id": f"#f{i}",
                 "property": "dateModified",
                 "message": "A File SHOULD have a dateModified"}
                for i in range(9)
            ],
        ]
        rows = re.findall(r"<li>.*?</li>", _render_recommendations(val, None), re.S)
        assert "licence" in rows[0], "the REQUIRED action must lead"
        assert "measurement technique" in rows[1], "impact outranks size within a tier"
        assert "dateModified" in rows[2]

    def test_no_meta_line_and_no_set_aside_bucket(self) -> None:
        """The "N actions clear M findings" meta line and the "left open on
        purpose" aside are gone on the owner's call (#607 design handoff)."""
        section = self._section()
        assert "actions clear" not in section
        assert "left open on purpose" not in section
        assert "na-aside" not in section


class TestTheOverflowLinePointsSomewhere:
    """"listed in full below" named a place without pointing at it — on a page
    this long "below" is several screens and three sections away."""

    def test_the_overflow_line_links_to_the_findings(self) -> None:
        import re

        from builder.tools.validation import ValidationReport
        from builder.writers.maturity_report import _render_recommendations

        val = ValidationReport()
        val.base_passed = True
        val.issue_records = [
            {"profile": "tox", "severity": "recommended", "entity_id": f"./#e{i}",
             "message": f"SHOULD have a thing of kind {i}"}
            for i in range(12)
        ]
        section = _render_recommendations(val, None)
        overflow = re.findall(r"<li>.*?</li>", section, re.S)[-1]
        assert "further action" in overflow, "no overflow row rendered; this test is inert"
        assert re.search(r"and \d+ further actions \(\d+ findings\)", overflow)
        assert 'href="#adherence"' in overflow

    def test_the_anchor_it_points_at_exists_on_the_page(self) -> None:
        """A link to an id nothing defines is a dead link that still looks fine
        in the markup — the same class of bug as a class with no CSS rule."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        assert 'id="adherence"' in page


class TestTheRecommendationsCloseTheReport:
    """Assessment first, then what to do about it: Recommendations follows the
    evidence sections and only References stands after it. The jump link that
    used to advertise it from the top is gone with the meta line (#606)."""

    def _page(self) -> str:
        state = vhps_fixture_state("S-VHPS21")
        state.validation.base_passed = True
        state.validation.issue_records = [
            {
                "profile": "base",
                "severity": "required",
                "entity_id": "./",
                "message": "The root Dataset MUST have a licence",
            },
            *[
                {
                    "profile": "tox",
                    "severity": "recommended",
                    "entity_id": f"./#e{i}",
                    "message": "A Sample SHOULD have a description",
                }
                for i in range(4)
            ],
        ]
        page = build_maturity_html(state)
        assert 'id="next"' in page, "no actions rendered; this test is inert"
        return page

    def test_the_recommendations_come_after_the_evidence(self) -> None:
        page = self._page()
        assert page.index("Profile adherence</h2>") < page.index("Recommendations</h2>")
        assert page.index("Reproducibility readiness</h2>") < page.index("Recommendations</h2>")
        assert page.index("Recommendations</h2>") < page.index('class="refs"')

    def test_no_jump_link_remains(self) -> None:
        page = self._page()
        assert 'class="jump"' not in page.split("</style>", 1)[-1]

    def test_no_section_when_there_is_nothing_to_do(self) -> None:
        """An empty exhortation is worse than silence."""
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        assert 'id="next"' not in page.split("</style>", 1)[-1]


class TestTheDataFilesRowCountsData:
    """"Data files included" reads the file classification, not any File at all.

    The row used to be ``bool(state.list_entities("File"))``, which a crate
    holding three protocols and no measurements satisfied. It now counts the
    files classified as data (#591) — and has to reach that class through the
    same classifier the rest of the build uses, because ``File.role`` is free
    text that predates the classification and outlives it.
    """

    @staticmethod
    def _row(*files: dict[str, str]) -> bool:
        from builder.state import Entity
        from builder.writers.maturity_report import _reproducibility_checks

        state = CrateState()
        for index, fields in enumerate(files):
            state.add_entity(Entity(entity_id=f"file_{index}", type="File", fields=dict(fields)))
        rows = _reproducibility_checks(state)
        return next(ok for label, ok, _ in rows if label == "Data files included")

    def test_a_crate_of_protocols_is_not_a_crate_with_data(self) -> None:
        assert not self._row(
            {"name": "SOP.docx", "dest_path": "data/SOP.docx"},
            {"name": "README.txt", "dest_path": "data/README.txt"},
        )

    def test_the_measurements_count(self) -> None:
        assert self._row(
            {"name": "SOP.docx", "dest_path": "data/SOP.docx"},
            {"name": "004043.csv", "dest_path": "data/004043.csv", "role": "raw_data_file"},
        )

    def test_a_session_saved_before_the_classification_still_counts(self) -> None:
        """The spine used to stamp ``raw_data``/``processed_data`` on every File.

        Those sessions resume without re-running discovery, so their crates carry
        the retired spelling forever. Read as a class it matches neither tier and
        the row went dark on a crate whose data was all present.
        """
        assert self._row(
            {"name": "004043.csv", "dest_path": "data/004043.csv", "role": "raw_data"},
            {"name": "Combined.xlsx", "dest_path": "data/Combined.xlsx", "role": "processed_data"},
        )

    def test_a_role_the_classification_does_not_use_is_not_a_class(self) -> None:
        """``role`` is free text — ``draft_file`` takes whatever the agent passes.

        A label the classifier never emits says nothing about which tier the file
        is, so the file is classified rather than taken at its word.
        """
        assert self._row({"name": "004043.csv", "dest_path": "data/004043.csv", "role": "figure"})
        assert not self._row({"name": "SOP.docx", "dest_path": "data/SOP.docx", "role": "figure"})
