"""Tests for the RO-Crate maturity report (#85)."""

from __future__ import annotations

import html
import re

import json
from pathlib import Path
from typing import Any

import pytest

from builder.state import CrateState, ValidationReport
from builder.tools.builder import build_crate, export_crate
from builder.writers.maturity_report import REPORT_FILENAME, build_maturity_html
from tests.fixtures.crate_graphs import tabbed_views_graph
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


def _markup(page: str) -> str:
    """The rendered markup without the stylesheet or any script body.

    Since #615 the page inlines React, React Flow, dagre and the crate's own
    payload. Those are megabytes of text that happen to contain ``id="…"``,
    ``class="…"`` and the literal ``<script>`` — so a structural assertion about
    the *document* has to read the document, not the programs it carries.
    """
    return re.sub(r"<script.*?</script>", "", _body(page), flags=re.S)


def _coverage(page: str) -> str:
    """The coverage inventory, which folds under the entity explorer's canvas.

    Not a section of its own: it inventories the same entities the explorer
    draws, and as a section it put a six-block contents list between the reader
    and the rest of the report.
    """
    # Up to the section's scripts, not to the fold's own closing tag: the Files
    # block nests one `<details>` per Dataset, so the first `</details>` inside
    # the body is a Dataset's and not the inventory's.
    return page.split('<div class="cov-all-body">', 1)[1].split("<script", 1)[0]


def _block(page: str, block_id: str) -> str:
    """One coverage block's markup, from its own div to the next block's.

    The blocks are siblings inside one fold (#618), so "up to the next
    ``</div>``" would stop inside the first table it meets; the boundary is the
    next block, or the end of the fold.
    """
    after = _coverage(page).split(f'id="{block_id}"', 1)[1]
    return re.split(r'<details class="cov" id=', after, maxsplit=1)[0]


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
        for heading in ("Profile adherence", "FAIR", "OECD MIT", "AI-readiness"):
            assert heading in page, f"missing section: {heading}"
        # Computed scores, not just the static labels:
        # "of N", not a hard-coded "of 5": the denominator is the highest level a
        # crate can REACH (Level 5 is scored entirely on hosting/enterprise
        # capability, which no RO-Crate can evidence). The assertion's point is that
        # a COMPUTED level is rendered, which it still makes.
        assert re.search(r"DSM level [0-5] of [1-5]", page), "no computed DSM level rendered"
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

    def test_a_title_slugged_into_the_identifier_does_not_headline(self) -> None:
        """A crate reached the report headlined
        `inv_neural_cell_screening_models_for_endocrine_disruption_of_thyroid_
        hormone_signaling` — a filename slug sitting in the root's `identifier`
        where a registry accession belongs (#628). The headline is what a reader
        cites, so it leads with something citable: a slug is not, and the crate's
        own title is."""
        state = CrateState()
        state.metadata.title = "Neural cell in vitro toxicology assays"
        state.metadata.accession = (
            "inv_neural_cell_screening_models_for_endocrine_disruption_of_"
            "thyroid_hormone_signaling"
        )

        page = build_maturity_html(state)

        assert "<h1>Neural cell in vitro toxicology assays</h1>" in page
        assert f"<h1>{state.metadata.accession}</h1>" not in page

    def test_the_browser_tab_follows_the_headline(self) -> None:
        """The tab is the same claim in a smaller place; a slug there is the
        first thing a reader sees of the crate."""
        state = CrateState()
        state.metadata.title = "A readable title"
        state.metadata.accession = "inv_" + "_".join(["word"] * 20)

        page = build_maturity_html(state)

        assert "<title>A readable title — vitro-crate maturity report</title>" in page

    def test_a_sentence_short_enough_to_fit_still_does_not_headline(self) -> None:
        """The other half of the rule. A prose phrase written into the
        identifier field is short enough to pass a length bound, and is still
        not something anyone can cite — one token is what makes an identifier."""
        state = CrateState()
        state.metadata.title = "The real title"
        state.metadata.accession = "thyroid assay set"

        page = build_maturity_html(state)

        assert "<h1>The real title</h1>" in page

    def test_a_real_accession_still_leads(self) -> None:
        """The rule refuses a slug, not an identifier. `S-VHPS21` is what people
        cite this deposit by, and it stays the headline."""
        page = build_maturity_html(self._state())

        assert "<h1>S-VHPS21</h1>" in page

    def test_a_doi_still_leads(self) -> None:
        """Long, but a citable identifier — the bound is not merely "short"."""
        state = CrateState()
        state.metadata.title = "A study"
        state.metadata.accession = "https://doi.org/10.1007/s00204-024-03787-2"

        page = build_maturity_html(state)

        assert f"<h1>{state.metadata.accession}</h1>" in page

    def test_an_identifier_that_does_not_headline_is_still_reported(self) -> None:
        """Demoted, never dropped: it is what the crate claims to be identified
        by, and a reader who cannot see it cannot question it."""
        state = CrateState()
        state.metadata.title = "A study"
        state.metadata.accession = "inv_" + "_".join(["word"] * 20)

        page = build_maturity_html(state)

        # Located by the card's own heading element, not by the words: the
        # stylesheet's comments name the card too, and splitting on the text
        # picked the CSS up instead.
        card = page.split('<div class="hcard-h">About this study</div>', 1)[1]
        card = card.split("</div>\n", 1)[0]
        assert f'<span class="hlabel">Identifier</span>{state.metadata.accession}' in card

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
        this above references") — not in the masthead, which keeps only the
        study card. The references close the page: the footer's two slogan
        spans were removed on review, and the footer went with them."""
        page = build_maturity_html(self._state())
        i_study = page.index('<div class="hcard-h">About this study</div>')
        i_kgrid = page.index('<div class="kgrid">')
        i_crate = page.index('<div class="hcard-h">About this RO-Crate</div>')
        i_refs = page.index('<div class="refs">')
        assert i_study < i_kgrid, "the study card left the masthead"
        assert i_kgrid < i_crate < i_refs, "wrong foot order"
        assert "<footer>" not in page
        assert "Self-contained · offline" not in page
        assert "Generated by vitro-crate" not in page

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

    def test_the_graph_tile_states_where_each_entity_lives(self) -> None:
        """The tile's four figures are the explorer payload's own residence
        tally (#720) — pinned to it the way the view chips are held to the
        coverage blocks — and the orphan ratio it used to headline is gone: the
        explorer's legend and the coverage blocks already report orphans."""
        from collections import Counter

        from builder.writers.entity_explorer import build_explorer_payload

        graph = {"@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": "T",
             "hasPart": [{"@id": "data/a.csv"}, {"@id": "#sample"}],
             "mentions": [{"@id": "https://www.wikidata.org/wiki/Q42"}],
             "author": [{"@id": "#nobody"}]},
            {"@id": "data/a.csv", "@type": "File", "name": "a.csv"},
            {"@id": "#sample", "@type": "Sample", "name": "a record"},
            {"@id": "https://www.wikidata.org/wiki/Q42", "@type": "DefinedTerm", "name": "Q42"},
            {"@id": "#orphan", "@type": "Sample", "name": "loose"},
        ]}
        page = build_maturity_html(CrateState(), graph=graph)
        tile = re.search(r'<span class="eyebrow">Graph</span></div>(.*?)</article>', page, re.S)
        assert tile
        shown = {
            key: int(n)
            for n, key in re.findall(
                r"<b>(\d+)</b> (carried|record|elsewhere|named)", tile.group(1)
            )
        }
        # The oracle is the shape rule, not the tile: `./` and the file are
        # carried, the two #fragments records, the IRI elsewhere, the author
        # nothing describes named — the orphan is a record like any other.
        assert shown == {"carried": 2, "record": 2, "elsewhere": 1, "named": 1}
        payload = build_explorer_payload(graph)
        assert shown == Counter(n["residence"] for n in payload["nodes"])
        assert f"<b>{len(payload['nodes'])}</b>" in tile.group(1), "the headline is the total"
        # The bar carries the same four counts as its segment widths, in the
        # same order, so the figure and its key cannot disagree.
        assert re.findall(r'<i class="(\w+)" style="flex-grow:(\d+)"', tile.group(1)) == [
            ("carried", "2"),
            ("record", "2"),
            ("elsewhere", "1"),
            ("named", "1"),
        ]
        assert "linked and retrieved" not in tile.group(1)
        assert '<span class="den">' not in tile.group(1), "no orphan ratio"
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
        # Over the markup: since #615 the page also carries the crate's own
        # metadata as JSON for the entity explorer, and a `javascript:` URL the
        # crate recorded is part of what the crate says. The guard is about what
        # the report turns into a link — the explorer builds no anchors at all,
        # which `test_a_crate_url_can_never_become_a_link` holds it to.
        assert "javascript:" not in _markup(page)
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

    def test_a_tier_nothing_in_the_stack_checks_is_neutral_not_green(
        self, monkeypatch
    ) -> None:
        """#620's guarantee, restated for a cumulative matrix.

        It used to be reachable through ISA's OPTIONAL column, because ISA
        declares no ``sh:Info`` shape of its own. Now that a row reports what it
        inherits, that column is answered by the base profile's twelve, and the
        state is only reached when NOTHING in the stack checks a tier — an
        unreadable profile registry, or a tier every layer drops. Empty is still
        never a green: a profile's silence is not the crate's cleanliness.
        """
        import builder.writers.maturity_report as report

        monkeypatch.setattr(
            report, "_tier_capability", lambda: {k: frozenset() for k, _ in report._PROFILE_LAYERS}
        )
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended", "optional"}

        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))

        for profile in ("base", "isa", "tox"):
            assert cells[f"{profile}-optional"] == ("na", "no checks defined at this level")

    def test_a_layer_cannot_pass_where_the_layer_it_extends_fails(self) -> None:
        """Conformance is cumulative, and the matrix has to say so.

        The profile is a three-layer stack in which each layer is adopted on top
        of the one below — "interoperability is inherited rather than rebuilt",
        so a conforming crate "is simultaneously a valid RO-Crate and an
        ISA-structured object". A real report showed ISA failing REQUIRED while
        ISA-Tox passed it, which under that architecture cannot happen: ISA-Tox
        was graded on its own 35 checks while ignoring the 140 it inherits.
        """
        val = ValidationReport(base_passed=True, isa_passed=False, tox_passed=True)
        val.assessed_tiers = {"required", "recommended", "optional"}
        val.issue_records = [
            {"profile": "isa", "severity": "required", "entity_id": "#a", "message": "m"},
        ]

        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))

        assert cells["isa-required"][0] == "no"
        assert cells["tox-required"][0] == "no", cells["tox-required"]

    def test_an_inherited_finding_shows_in_the_layers_above_it(self) -> None:
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended", "optional"}
        val.issue_records = [
            {"profile": "base", "severity": "recommended", "entity_id": "#a", "message": "m"},
        ]

        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))

        assert cells["base-recommended"][0] == "no"
        assert cells["isa-recommended"][0] == "no", cells["isa-recommended"]
        assert cells["tox-recommended"][0] == "no", cells["tox-recommended"]

    def test_the_optional_level_is_inherited_rather_than_absent(self) -> None:
        """ISA and ISA-Tox declare no MAY shape of their own, but they extend a
        profile that does — so their OPTIONAL cell reports what they inherit,
        not a dash. A dash there reads as "this level does not apply", which is
        false for a profile whose conformance includes RO-Crate's."""
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended", "optional"}

        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))

        assert cells["base-optional"] == ("ok", "no findings at this level")
        assert cells["isa-optional"][0] == "ok", cells["isa-optional"]
        assert cells["tox-optional"][0] == "ok", cells["tox-optional"]

    def test_an_inherited_optional_finding_fails_the_rows_that_inherit_it(self) -> None:
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended", "optional"}
        val.issue_records = [
            {"profile": "base", "severity": "optional", "entity_id": "#a", "message": "m"},
        ]

        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))

        assert cells["isa-optional"][0] == "no", cells["isa-optional"]
        assert cells["tox-optional"][0] == "no", cells["tox-optional"]

    def test_the_title_says_when_the_findings_were_inherited(self) -> None:
        """A reader fixing ISA-Tox needs to know the failure is not ISA-Tox's."""
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended", "optional"}
        val.issue_records = [
            {"profile": "isa", "severity": "recommended", "entity_id": "#a", "message": "m"},
        ]

        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))

        assert "inherited" in cells["tox-recommended"][1], cells["tox-recommended"]
        assert "inherited" not in cells["isa-recommended"][1], cells["isa-recommended"]

    def test_the_base_row_still_reports_only_itself(self) -> None:
        """It is the bottom of the stack: there is nothing beneath it to inherit."""
        val = ValidationReport(base_passed=True, isa_passed=False, tox_passed=False)
        val.assessed_tiers = {"required", "recommended", "optional"}
        val.issue_records = [
            {"profile": "isa", "severity": "required", "entity_id": "#a", "message": "m"},
            {"profile": "tox", "severity": "required", "entity_id": "#b", "message": "m"},
        ]

        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))

        assert cells["base-required"] == ("ok", "no findings at this level")

    def test_the_profile_cards_agree_with_the_matrix(self) -> None:
        """The REQUIRED cards are the same claim in a second place, so they
        inherit the same way. A card reading "ISA-Tox met" beside a matrix cell
        reading "ISA-Tox not met" would be the report contradicting itself."""
        import re as _re

        val = ValidationReport(base_passed=True, isa_passed=False, tox_passed=True)
        val.assessed_tiers = {"required"}

        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val)
        cards = _re.findall(
            r'<div class="prof-card"><span class="mk (\w+)"[^>]*>[^<]*</span><span>([^<]+)</span>',
            page,
        )
        by_name = {name: mark for mark, name in cards}

        assert by_name, page[:400]
        assert by_name["ISA"] == "no"
        assert by_name["ISA-Tox"] == "no", by_name

    def test_a_finding_outranks_the_no_checks_state(self) -> None:
        """A finding filed at a tier the profile defines no checks at — a
        checkpoint written against a profile version that had MAY rules, a local
        checker filing there — still reads as a failure. "No checks defined" may
        never swallow a finding somebody has to act on."""
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended", "optional"}
        val.issue_records = [
            {"profile": "isa", "severity": "optional", "entity_id": "#a", "message": "m"},
        ]
        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))
        assert cells["isa-optional"] == ("no", "1 finding at this level")

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
        # And it carries: ISA and ISA-Tox are adopted on top of RO-Crate, so
        # neither can be conformant at a tier RO-Crate failed. The cell says the
        # failure is not its own.
        assert cells["isa-required"] == ("no", "a profile it extends did not pass")
        assert cells["tox-required"] == ("no", "a profile it extends did not pass")

    def test_a_pre_records_verdict_still_counts_by_profile(self) -> None:
        """A checkpoint written before ``issue_records`` existed carries only
        flat display strings — their own ``[profile]`` prefix is what the
        matrix counts, so the titles stay true rather than claiming zero."""
        val = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        val.assessed_tiers = {"required", "recommended"}
        val.should_issues = ["[isa] #a: SHOULD have x", "[isa] #b: SHOULD have y",
                             "[base] ./: SHOULD have z"]
        cells = self._cells(build_maturity_html(vhps_fixture_state("S-VHPS21"), validation=val))
        assert cells["base-recommended"] == ("no", "1 finding at this level")
        assert cells["isa-recommended"] == ("no", "3 findings at this level, 1 inherited")
        assert cells["tox-recommended"] == ("no", "3 findings at this level, 3 inherited")

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

    def test_the_next_rung_is_filled_to_that_levels_own_completeness(self) -> None:
        """The bar under a DSM-labelled ladder must be a DSM number.

        It used to be filled from the RDA indicator set, so an empty crate showed a
        12%-filled rung captioned "1 of 8 FAIR indicators met" beneath a DSM level.
        """
        from builder.tools.fair_assessment import assess_fair_maturity, dsm_ceiling, dsm_grid
        from builder.tools.mit_assessment import assess_mit_coverage

        state = vhps_fixture_state("S-VHPS21")
        mit = assess_mit_coverage(state)
        fair = assess_fair_maturity(state, mit=mit)
        nxt = dsm_grid(state)[fair.dsm_level + 1]["TOTAL"]
        page = build_maturity_html(state)
        assert f"<b>{fair.dsm_level}</b>" in page
        assert page.count('<span class="rung2 done"></span>') == fair.dsm_level
        next_rung = re.search(r'<span class="rung2 next"[^>]*><i style="width:(\d+)%">', page)
        assert next_rung and int(next_rung.group(1)) == round(nxt["published_pct"])
        assert f'title="{nxt["met"]} of {nxt["total"]} indicators at that level"' in page
        blockers = dsm_ceiling(state)["blocked_by"]
        assert blockers, "fixture has no DSM blockers; the assertion below is inert"
        assert (
            f'<a class="blockers" href="#next"><b>{len(blockers)} '
            f"indicator{'s' if len(blockers) != 1 else ''}</b> "
            f"to level {fair.dsm_level + 1}</a>" in page
        )
        # The count is drillable, and what it drills into is an instruction rather
        # than a restatement of the model's question.
        for bid, text, _why in blockers:
            assert text in page, text
            # The chip names the indicator, linked to the model's own entry for it,
            # beside the published wording — chipped like a validator finding.
            assert f"#{bid.lower()}" in page, bid
            assert f"{bid}</a> &middot; {html.escape(text)}" in page, "chipped like a finding"

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

    def test_the_rose_tile_carries_no_iuclid_chip(self) -> None:
        """The tile's header is the principle eyebrow alone: the "IUCLID DB"
        link chip that once sat beside it is gone, assessed or not (owner's
        review, #606 lane). The orphaned `.chip-link` rule leaves with it."""
        from builder.state import MITReport
        from builder.writers.maturity_report import _CSS_PATH, _mit_rose_tile

        assessed = _mit_rose_tile(
            MITReport(
                module_scores={"Test System": {"completed": 1, "total": 2}},
                overall_score=0.5,
            )
        )
        unassessed = _mit_rose_tile(MITReport())
        for tile in (assessed, unassessed):
            assert "IUCLID" not in tile
            assert "iuclid6.echa.europa.eu" not in tile
            assert "chip-link" not in tile
            assert '<span class="eyebrow">FAIR principle 1.3' in tile
        assert ".chip-link" not in _CSS_PATH.read_text(encoding="utf-8")

    def test_the_footnote_superscripts_resolve(self) -> None:
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"))
        for fn in ("fn-dsm", "fn-mit", "fn-air"):
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
            ".mat .meter, .mat .meter *, .mat .rung2, .mat .rung2 > i, .mat .airbar i"
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
        assert "AI-readiness" in body
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
        panel = _block(page, "cov-data")
        assert '<summary class="cov-h">Files' in page
        rows = re.findall(r"<tr><th scope=\"row\">.*?</tr>", panel, re.S)
        assert len(rows) == 2
        assert "loose.csv" in rows[0] and 'class="mk no"' in rows[0]
        assert "measurements_raw.csv" in rows[1]
        assert "text/csv" in rows[1] and "2 KB" in rows[1]
        # The muted span states the file's PATH — its @id in the crate — not a
        # kind word (review comment: "should state the file paths"). A file
        # whose display name IS its path gets no duplicate span.
        assert '<span class="ty">a.csv</span>' in rows[1]
        assert "raw data" not in panel
        assert '<span class="ty">' not in rows[0]
        assert "1 of 2 data entities cannot be reached" in panel

    def test_no_data_entities_drops_the_tab(self) -> None:
        graph = {"@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": "T"},
        ]}
        page = build_maturity_html(CrateState(), graph=graph)
        assert 'id="cov-data"' not in page.split("</style>", 1)[-1]

    # --- grouped by the Dataset that lists the file (owner's review) ---------

    @staticmethod
    def _panel(page: str) -> str:
        return _block(page, "cov-data")

    @staticmethod
    def _folds(panel: str) -> list[tuple[str, str]]:
        """``(summary, body)`` per Dataset fold, in page order."""
        return re.findall(
            r'<details class="ds-fold"[^>]*><summary[^>]*>(.*?)</summary>(.*?)</details>',
            panel,
            re.S,
        )

    def _nested_graph(self) -> dict:
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "name": "Inv",
                 "hasPart": [{"@id": "#study"}, {"@id": "a.csv"}, {"@id": "b.csv"}]},
                {"@id": "#study", "@type": "Dataset", "additionalType": "Study",
                 "name": "The study", "hasPart": [{"@id": "#assay"}]},
                {"@id": "#assay", "@type": "Dataset", "additionalType": "Assay",
                 "name": "The assay", "hasPart": [{"@id": "b.csv"}]},
                {"@id": "raw/", "@type": "Dataset", "name": "raw folder",
                 "hasPart": [{"@id": "a.csv"}]},
                {"@id": "a.csv", "@type": "File", "name": "a.csv"},
                {"@id": "b.csv", "@type": "File", "name": "b.csv", "encodingFormat": "text/csv"},
                {"@id": "loose.csv", "@type": "File", "name": "loose.csv"},
            ]
        }

    def test_every_dataset_gets_a_fold_listing_the_files_it_has_part(self) -> None:
        """One fold per ``Dataset`` — ISA level first, then the name and how many
        files it lists — and a file under every Dataset whose ``hasPart`` names
        it: the root lists the whole tree, an Assay lists its own, and the
        reader sees both claims rather than an invented single owner."""
        page = build_maturity_html(CrateState(), graph=self._nested_graph())
        folds = self._folds(self._panel(page))
        by_name = {re.sub(r"<[^>]+>", "", summ): (summ, body) for summ, body in folds}
        names = [re.sub(r"<[^>]+>", "", summ) for summ, _ in folds]
        # ISA levels in backbone order, then plain folder Datasets.
        assert names == [
            "Not listed by any Dataset 1 file",
            "Investigation Inv 2 files",
            "Study The study 0 files",
            "Assay The assay 1 file",
            "Dataset raw folder 1 file",
        ], names
        inv_summ, inv_body = by_name["Investigation Inv 2 files"]
        assert '<span class="ds-lvl">Investigation</span>' in inv_summ
        assert "a.csv" in inv_body and "b.csv" in inv_body and "loose.csv" not in inv_body
        _, assay_body = by_name["Assay The assay 1 file"]
        assert "b.csv" in assay_body and "a.csv" not in assay_body
        assert "text/csv" in assay_body  # the row keeps its facts in every fold
        _, study_body = by_name["Study The study 0 files"]
        assert "<tr" not in study_body  # a Dataset that lists only containers
        _, loose_body = by_name["Not listed by any Dataset 1 file"]
        assert "loose.csv" in loose_body and 'class="mk no"' in loose_body

    def test_the_unlisted_group_is_absent_when_every_file_has_a_dataset(self) -> None:
        page = build_maturity_html(CrateState(), graph=self._graph())
        panel = self._panel(page)
        assert "Not listed by any Dataset" in panel  # loose.csv in the base graph
        graph = self._graph()
        graph["@graph"] = [n for n in graph["@graph"] if n["@id"] != "loose.csv"]
        page = build_maturity_html(CrateState(), graph=graph)
        panel = self._panel(page)
        assert "Not listed by any Dataset" not in panel
        assert [re.sub(r"<[^>]+>", "", s) for s, _ in self._folds(panel)] == [
            "Investigation T 1 file"
        ]

    def test_the_folds_open_by_default_and_the_css_styles_them(self) -> None:
        from builder.writers.maturity_report import _CSS_PATH

        page = build_maturity_html(CrateState(), graph=self._graph())
        panel = self._panel(page)
        assert panel.count('<details class="ds-fold" open>') == len(self._folds(panel)) == 2
        css = _CSS_PATH.read_text(encoding="utf-8")
        assert "details.ds-fold > summary" in css and ".ds-lvl" in css


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
        panel = _block(page, "cov-chem")
        table = re.search(r'<table class="chem-tbl">.*?</table>', panel, re.S)
        assert table, "no identification matrix"
        rows = [r for r in re.findall(r"<tr>.*?</tr>", table.group(0), re.S) if "Aflatoxin B1" in r]
        assert len(rows) == 1, "expected exactly one matrix row for the compound"
        return rows[0]

    def test_section_renders_the_note_and_the_matrix(self) -> None:
        page = self._page()
        assert '<details class="cov" id="cov-chem">' in page
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
        assert "🔗" not in _block(page, "cov-chem")

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
        assert 'id="cov-chem"' not in body
        assert 'for="mv-chem"' not in body
        assert '<span class="eyebrow">Chemicals</span>' not in body

    def test_no_graph_omits_the_section(self) -> None:
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21")))
        assert 'id="cov-chem"' not in body
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
        panel = page.split('<details class="cov" id="cov-chem">', 1)[1].split("</div>", 1)[0]
        assert "<script" not in panel.lower()
        assert "src=" not in panel and "@import" not in panel


class TestTheCoverageBlocksFoldDown:
    """#629: the section is an inventory of a whole crate, and it sat between
    the reader and everything below it."""

    def _page(self) -> str:
        return build_maturity_html(
            vhps_fixture_state("S-VHPS21"), graph=tabbed_views_graph()
        )

    def _blocks(self, page: str) -> list[str]:
        return re.findall(r"<details class=\"cov\"[^>]*>.*?</details>", _coverage(page), re.S)

    def test_every_block_is_a_fold(self) -> None:
        page = self._page()

        assert self._blocks(page), "no block folded"
        assert '<div class="cov" id=' not in _coverage(page), "a block stayed open by construction"

    def test_the_inventory_itself_folds_under_the_explorer(self) -> None:
        """It inventories the entities the canvas above draws, so it belongs to
        that section rather than standing between the reader and the rest of the
        report."""
        page = self._page()

        assert '<section class="coverage">' not in page
        assert '<details class="cov-all">' in page
        explorer = page.split('<section class="explorer"', 1)[1].split("</section>", 1)[0]
        assert '<details class="cov-all">' in explorer

    def test_the_summary_carries_the_name_and_the_count(self) -> None:
        """A closed block shows only its summary, so the count has to be in it —
        that number is the whole value of a block a reader has not opened."""
        for block in self._blocks(self._page()):
            summary = re.search(r"<summary[^>]*>(.*?)</summary>", block, re.S)
            assert summary, block[:80]
            if '<span class="cov-n">' in block:
                assert '<span class="cov-n">' in summary.group(1)

    def test_a_nested_fold_still_works(self) -> None:
        """The Files block already nests one ``<details>`` per Dataset. Folding
        the block around them must not swallow those: ``<details>`` nests, and
        the inner ones keep their own state."""
        page = self._page()
        files = next(b for b in self._blocks(page) if 'id="cov-data"' in b)

        assert 'class="ds-fold"' in files

    def test_print_opens_every_block(self) -> None:
        """The report prints. A printed copy that lost its inventory to a closed
        fold would be worse than the scrolling this fixes — the same reason the
        severity folds already force themselves open on paper."""
        from builder.writers.maturity_report import _load_css

        css = _load_css()
        printed = css.split("@media print", 1)[1]

        # The rule itself, not merely its words: `details.cov` and
        # `display:block !important` each appear in the print block for other
        # reasons, so asserting them separately passed with the rule deleted.
        assert re.search(
            r"details\.cov\s*>\s*\.cov-body\s*\{[^}]*display:\s*block\s*!important",
            printed,
        ), "print does not force the coverage folds open"

    def test_the_fold_classes_are_styled(self) -> None:
        """Same guard the report applies to itself: a class with no rule renders
        at browser defaults, which for a summary means a stray disclosure
        triangle beside a heading that already has one."""
        from builder.writers.maturity_report import _load_css

        css = _load_css()
        for cls in ("cov-body",):
            assert f".{cls}" in css, cls


class TestCellLinesPanel:
    """The Biological Samples view (#85): the biological test system, and
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
        panel = _block(page, "cov-chem")
        table = re.search(r'<table class="chem-tbl">.*?</table>', panel, re.S)
        assert table, "no identification matrix"
        rows = [r for r in re.findall(r"<tr>.*?</tr>", table.group(0), re.S) if "Aflatoxin B1" in r]
        assert len(rows) == 1, "expected exactly one matrix row for the compound"
        return rows[0]

    def test_renders_diagram_and_matrix(self) -> None:
        page = self._page()
        assert '<details class="cov" id="cov-cell">' in page
        assert '<summary class="cov-h">Biological models' in page
        assert "CHO-K1" in page
        assert "CVCL_0214" in page
        for column in ("RRID", "Type", "Organ", "Tissue", "Passage"):
            assert f">{column}</th>" in page, f"missing coverage column: {column}"

    def test_the_view_is_named_biological_models_wherever_a_reader_sees_it(self) -> None:
        """The owner's term for the test system is the checklist's — "biological
        model", as in the MIT module Biological Model Information — so the block
        heading and the table's corner header both say it (the legend and the
        diagram that also did are gone, #618). Until #625 this test's own name
        and reasoning said "biological model" while its assertions still pinned
        "Biological Samples"; the rename settles which of the two was right.
        "Cell line" survives only where
        it names the actual declaration being checked: the Type column's
        tooltip (the entity is typed as a cell line) and ``CellLine`` itself.
        """
        page = self._page()
        assert '<summary class="cov-h">Biological models' in page
        assert '<th scope="col">Biological model</th>' in page
        panel = _block(page, "cov-cell")
        rest = panel.replace('title="Typed as a cell line"', "")
        assert "cell line" not in rest.lower(), "a reader-facing 'cell line' survived"

    def test_the_panel_carries_no_summary_line(self) -> None:
        """Review comment: the "The biological test system — …" caption is
        gone; the panel opens with the diagram. The total still shows in the
        tab badge, and the warnings still call out wiring and RRID gaps."""
        page = self._page()
        panel = page.split('id="cov-cell"', 1)[1].split('<div class="panel"', 1)[0]
        assert '<p class="prov-cap">' not in panel
        assert "biological test system" not in page.lower()

    def test_unconsumed_model_is_called_out_with_the_fix(self) -> None:
        page = self._page(wire=False)
        assert "1 of 1 biological samples are not consumed by any process." in page
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
        assert "1 of 1 biological samples carry no Cellosaurus RRID." in page

    def test_crate_without_cell_lines_omits_the_view(self) -> None:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#f"}]},
                {"@id": "#f", "@type": "File", "name": "result.csv"},
            ]
        }
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph))
        assert 'id="cov-cell"' not in body
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
        panel = _block(page, "cov-chem")
        table = re.search(r'<table class="chem-tbl">.*?</table>', panel, re.S)
        assert table, "no identification matrix"
        rows = [r for r in re.findall(r"<tr>.*?</tr>", table.group(0), re.S) if "Aflatoxin B1" in r]
        assert len(rows) == 1, "expected exactly one matrix row for the compound"
        return rows[0]

    def test_renders_diagram_and_matrix(self) -> None:
        page = self._page()
        assert '<details class="cov" id="cov-people">' in page
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
        assert 'id="cov-people"' not in page

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
        panel = _block(page, "cov-chem")
        table = re.search(r'<table class="chem-tbl">.*?</table>', panel, re.S)
        assert table, "no identification matrix"
        rows = [r for r in re.findall(r"<tr>.*?</tr>", table.group(0), re.S) if "Aflatoxin B1" in r]
        assert len(rows) == 1, "expected exactly one matrix row for the compound"
        return rows[0]

    def test_renders_diagram_and_matrix(self) -> None:
        page = self._page()
        assert '<details class="cov" id="cov-cite">' in page
        assert '<summary class="cov-h">Citations' in page
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

    def test_crate_without_articles_omits_the_view(self) -> None:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#f"}]},
                {"@id": "#f", "@type": "File", "name": "result.csv"},
            ]
        }
        body = _body(build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph))
        assert 'id="cov-cite"' not in body
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


class TestAnUnreadableInstrumentIsSaidNotHidden:
    """A missing `air/criteria.yaml` must not silently delete a KPI tile.

    `assess_air_readiness` returns an empty `AIRReport` rather than raising, so the
    other three axes still render — which is right. But a tile that quietly vanishes
    reads to a viewer as a report with four axes, not as one whose fifth could not be
    scored, and the whole point of this axis is that "not assessed" is stated.
    """

    def test_the_tile_says_not_assessed_rather_than_disappearing(self) -> None:
        from builder.state import AIRReport
        from builder.writers.maturity_report import _air_tile

        tile = _air_tile(AIRReport())
        assert "AI-readiness" in tile
        assert "not assessed" in tile

    def test_the_section_is_omitted_entirely(self) -> None:
        """The section is detail; with nothing to detail there is nothing to show."""
        from builder.state import AIRReport
        from builder.writers.maturity_report import _render_air_section

        assert _render_air_section(AIRReport()) == ""


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
        assert page.index("the Bridge2AI profile</h2>") < page.index("Recommendations</h2>")
        assert page.index("Recommendations</h2>") < page.index('class="refs"')

    def test_no_jump_link_remains(self) -> None:
        page = self._page()
        assert 'class="jump"' not in page.split("</style>", 1)[-1]

    def test_no_section_when_there_is_nothing_to_do(self) -> None:
        """An empty exhortation is worse than silence.

        Asserted against the renderer rather than a fixture crate: every crate on hand
        has a DSM indicator open, and those are now rows in this section, so "nothing
        to do" is a state the corpus no longer reaches.
        """
        from builder.writers.maturity_report import _render_recommendations

        assert _render_recommendations(None, None, dsm=[], dsm_level=0) == ""

    def test_a_maturity_gap_alone_still_earns_the_section(self) -> None:
        """A crate that validates cleanly can still be one indicator off the next level,
        and that is exactly the reader who needs to be told what to do."""
        from builder.tools.remediation import Action
        from builder.writers.maturity_report import _render_recommendations

        action = Action(
            key="dsm:DSM-1-C0",
            kind="indicator",
            subject="DSM-1-C0",
            findings=["no persistent identifier"],
            tier="MATURITY",
            source="dsm",
            message="Each Dataset purposed for sharing and re-use is assigned a unique identifier",
            instruction="Mint a DOI for the deposit and record it on the root as `identifier`.",
            consequence="a reader cannot cite the deposit",
        )
        page = _render_recommendations(None, None, dsm=[action], dsm_level=1)
        assert 'id="next"' in page
        assert "DSM-1-C0</a> &middot; Each Dataset" in page, "the chip names and links it"
        assert '<span class="rec-badge lvl">Level 1</span>' in page
        assert "Mint a DOI" in page and "a reader cannot cite the deposit" in page


