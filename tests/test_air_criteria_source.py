"""``air/criteria.yaml`` is generated from the published Bridge2AI sources, not hand-written.

The NIH Bridge2AI *AI-readiness Criteria for Biomedical Data* is a published instrument
with two distributions under two different licences, and this repo vendors both:

* the article's JATS XML (CC BY-ND 4.0) — the *Practice* sentences, carried verbatim
  because ND permits unadapted redistribution and forbids exactly the paraphrase one
  would otherwise be tempted to write;
* the authors' self-evaluation worksheet (CC BY 4.0) — the criterion ids, the short
  labels, and the scoring arithmetic.

These tests pin the generated file to both, so a re-vendoring that changes a word, an
id, or a count fails loudly rather than silently restating a 32-author instrument in
our own terms. They mirror ``tests/test_dsm_indicators_source.py``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
CRITERIA_YAML = REPO / "air" / "criteria.yaml"
JATS_XML = REPO / "air" / "bridge2ai_ai_readiness_v6.jats.xml"
WORKSHEET = REPO / "air" / "bridge2ai_worksheet_v1.0.0.xlsx"
GENERATOR = REPO / "scripts" / "gen_air_criteria.py"

# Table 1 of bioRxiv v6, counted row by row. The paper never states a total itself,
# so "28" is ours by derivation and is pinned here rather than quoted as their claim.
DIMENSION_SIZES = {0: 4, 1: 4, 2: 5, 3: 3, 4: 4, 5: 4, 6: 4}
TOTAL_CRITERIA = 28

# The vendored bytes. The worksheet's md5 is the one Zenodo publishes for the record.
JATS_SHA256 = "6e68415f699b0e605e082c2b660cdc36a7b69f0e5db1884e208f5323164ac5ee"
WORKSHEET_MD5 = "ea7690d1b3b2ead58c2436e7723f6ecc"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_air_criteria", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen():
    return _load_generator()


@pytest.fixture(scope="module")
def data() -> dict:
    return yaml.safe_load(CRITERIA_YAML.read_text())


@pytest.fixture(scope="module")
def criteria(data) -> list[dict]:
    return data["criteria"]


class TestGeneratedFromTheSource:
    """The committed YAML is exactly what the generator produces — no hand edits."""

    def test_the_vendored_sources_are_present(self):
        assert JATS_XML.exists(), "the CC BY-ND article XML carrying the Practice text"
        assert WORKSHEET.exists(), "the CC BY 4.0 scoring worksheet"

    def test_the_article_bytes_are_the_ones_we_read(self):
        digest = hashlib.sha256(JATS_XML.read_bytes()).hexdigest()
        assert digest == JATS_SHA256, (
            "the vendored article changed. A preprint can be re-posted with renumbered "
            "or reworded criteria — re-run the generator and re-derive the pins "
            "deliberately rather than relaxing this test."
        )

    def test_the_worksheet_is_the_published_zenodo_file(self):
        assert hashlib.md5(WORKSHEET.read_bytes()).hexdigest() == WORKSHEET_MD5

    def test_committed_yaml_equals_generator_output(self, gen, data):
        assert data == gen.build_data(), (
            "air/criteria.yaml is stale or hand-edited. "
            "Regenerate: uv run python scripts/gen_air_criteria.py"
        )


class TestTheWholePublishedInstrumentIsCarried:
    """All 28 criteria, including the ones no crate can evidence."""

    def test_every_criterion_is_present(self, criteria):
        assert len(criteria) == TOTAL_CRITERIA

    def test_the_seven_dimensions_have_their_published_sizes(self, criteria):
        sizes: dict[int, int] = {}
        for crit in criteria:
            sizes[crit["dimension"]] = sizes.get(crit["dimension"], 0) + 1
        assert sizes == DIMENSION_SIZES

    def test_dimension_three_is_explainability_not_ethics(self, data):
        """The abstract lists the dimensions in a different order than Table 1.

        Table 1 is authoritative — it is what the criterion ids and the worksheet's
        radar axes use. Following the abstract would swap 3 and 4 and silently
        mislabel every Ethics and Explainability figure in the report.
        """
        assert data["dimensions"][3] == "Pre-model Explainability"
        assert data["dimensions"][4] == "Ethics"

    def test_ids_follow_the_paper_not_the_worksheet(self, criteria):
        """Worksheet v1.0.0 predates paper v6 and numbers dimension 3 differently."""
        explainability = [c["id"] for c in criteria if c["dimension"] == 3]
        assert explainability == ["3.a", "3.b", "3.c"]

    def test_every_worksheet_divergence_is_recorded_explicitly(self, criteria):
        """Where the worksheet's id differs from the paper's, both are carried.

        Silently dropping the worksheet id would make our numbers impossible to line
        up against the authors' own spreadsheet — the one artefact a reader can run.
        """
        divergent = {c["id"]: c["worksheet_id"] for c in criteria if "worksheet_id" in c}
        assert divergent == {"3.b": "3.c", "3.c": "3.d"}


class TestTextIsVerbatim:
    """CC BY-ND permits copying, not rewording. Verbatim is the licence-safe choice."""

    def test_every_practice_appears_verbatim_in_the_article(self, criteria):
        """Not "close to" — the whole sentence, character for character.

        The generator drops JATS ``<xref>`` citation superscripts, which would
        otherwise glue bibliography numbers onto the prose. Nothing else is touched,
        so every practice must survive a naive tag-strip of the article unchanged.
        """
        import re

        flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", JATS_XML.read_text(encoding="utf-8")))
        for crit in criteria:
            assert crit["text"] in flat, (
                f"{crit['id']}: practice text is not in the vendored article verbatim — "
                "it has been paraphrased, which is the one thing CC BY-ND forbids."
            )

    def test_the_labels_are_the_papers_and_the_worksheet_agrees(self, criteria):
        """Where paper and worksheet label the same criterion, both are carried."""
        findable = next(c for c in criteria if c["id"] == "0.a")
        assert findable["label"] == "Findable"
        assert findable["worksheet_label"] == "Findable"

    def test_the_footnote_qualifying_the_table_is_carried(self, data):
        assert "most relevant relationship" in data["source"]["table_note"]


class TestProvenanceIsRecorded:
    """A reader must be able to get from the YAML to the instrument and its licence."""

    def test_the_practice_text_licence_is_the_articles(self, data):
        assert data["source"]["license"] == "CC-BY-ND-4.0"

    def test_the_worksheet_licence_is_separate_and_is_not_nd(self, data):
        assert data["source"]["worksheet"]["license"] == "CC-BY-4.0"

    def test_the_article_is_identified_three_ways(self, data):
        article = data["source"]
        assert article["doi"] == "10.1101/2024.10.23.619844"
        assert article["pmid"] == 39484409
        assert article["pmcid"] == "PMC11526931"

    def test_the_version_is_pinned_because_a_preprint_moves(self, data):
        assert data["source"]["version"] == 6
        assert data["source"]["posted"] == "2026-04-24"

    def test_no_peer_reviewed_version_is_claimed(self, data):
        """It is still a preprint. Implying otherwise would misrepresent it."""
        assert data["source"]["status"] == "preprint"
        assert data["source"]["peer_reviewed"] is None

    def test_the_worksheet_doi_is_the_version_doi(self, data):
        assert data["source"]["worksheet"]["doi"] == "10.5281/zenodo.13961091"


class TestTheScoringModelIsTheAuthors:
    """Seven percentages, no aggregate — the authors refuse one and so do we."""

    def test_the_worksheet_formula_is_recorded(self, data):
        assert data["scoring"]["per_dimension"] == "met / total * 100"

    def test_there_is_no_aggregate_score(self, data):
        """Verbatim: "We do not score it pass/fail overall"."""
        assert data["scoring"]["aggregate"] is None
        assert "do not score it pass/fail overall" in data["scoring"]["note"]

    def test_the_tri_state_denominator_is_declared_as_a_deviation(self, data):
        """The published denominator counts every criterion; ours can drop one.

        Reporting only our number would quietly restate the instrument. Both are
        computed, and the YAML says which is which.
        """
        assert data["scoring"]["published_denominator"] == "all criteria in the dimension"
        assert data["scoring"]["local_denominator"] == "criteria assessed (a declared deviation)"


class TestLocalScopeIsHonest:
    """What we claim to assess, and what we refuse to guess at."""

    def test_every_scoped_criterion_names_a_registered_check(self, criteria):
        from builder.tools.air_assessment import AIR_CHECKS

        for crit in criteria:
            if crit["scope"] in ("full", "partial"):
                assert crit["check"] in AIR_CHECKS, f"{crit['id']} names an unknown check"

    def test_an_unassessed_criterion_names_no_check(self, criteria):
        for crit in criteria:
            if crit["scope"] == "na":
                assert "check" not in crit, f"{crit['id']} is na but carries a check"

    def test_consent_and_governance_are_never_auto_passed(self, criteria):
        """A crate cannot evidence IRB approval or a data-access committee.

        Passing one would be the single most embarrassing output this axis could
        produce, so they are `na` — reported, excluded from the denominator, and
        never counted either way.
        """
        by_id = {c["id"]: c for c in criteria}
        for ident in ("4.a", "4.b", "4.c", "5.a", "5.b", "5.c", "6.b"):
            assert by_id[ident]["scope"] == "na", f"{ident} claims to be assessable"

    def test_the_criterion_ro_crate_answers_is_assessed(self, criteria):
        """5.d names RO-Crate as its own suggested resource — we had better score it."""
        associated = next(c for c in criteria if c["id"] == "5.d")
        assert associated["scope"] in ("full", "partial")
        assert "RO-Crate" in associated["suggested_resources"]

    def test_a_reused_check_records_which_indicator_it_is_shared_with(self, criteria):
        """Where AIR asks a question DSM or RDA already asks, it calls the same check.

        Two implementations of one question is how the axes come to disagree, so the
        overlap is recorded rather than hidden — the paper cannot present these axes
        as independent evidence.
        """
        overlapping = {c["id"]: c["overlaps"] for c in criteria if c.get("overlaps")}
        assert overlapping, "no overlap recorded — the axes are not that independent"
        for ident, refs in overlapping.items():
            assert all(r.startswith(("DSM-", "RDA-")) for r in refs), ident

    def test_an_overlap_says_whether_the_implementation_is_actually_shared(self, criteria):
        """"Shares a check" and "asks the same thing" are different claims.

        Reusing a check is the right default — but 18 of the 40 RDA checks are
        `len(entities) > 0` presence tautologies, and importing one into a new axis
        would launder a known-bad measurement. Where that is the reason, the YAML
        says so rather than implying a reuse that did not happen.
        """
        kinds = {c["overlap_kind"] for c in criteria if c.get("overlaps")}
        assert kinds <= {"shared-check", "same-question"}
        assert "shared-check" in kinds

    def test_a_shared_check_really_is_the_same_function(self, criteria):
        from builder.tools.air_assessment import AIR_CHECKS
        from builder.tools.fair_assessment import DSM_CHECKS, FAIR_CHECKS

        def unwrap(fn):
            """A state-only check is wrapped for the (state, graph) shape."""
            return getattr(fn, "__wrapped_check__", fn)

        others = {id(unwrap(fn)) for fn in (*DSM_CHECKS.values(), *FAIR_CHECKS.values())}
        shared = [c for c in criteria if c.get("overlap_kind") == "shared-check"]
        assert shared
        for crit in shared:
            target = unwrap(AIR_CHECKS[crit["check"]])
            assert id(target) in others, (
                f"{crit['id']} claims to share {crit['overlaps']}'s check but "
                "carries its own implementation"
            )


class TestRemediesCannotClaimMoreThanTheLoopCanDo:
    """Each criterion declares how it would be fixed; the generator refuses fictions."""

    def test_every_criterion_declares_a_route(self, criteria):
        routes = {c["remedy"]["route"] for c in criteria}
        assert routes <= {"auto", "ask-user", "draft", "report-only"}

    def test_no_remedy_asks_a_human_for_an_identifier(self, criteria):
        """D5 — identifiers come from lookups, so the answer would be discarded."""
        from builder.tools.field_kinds import is_identifier_field

        for crit in criteria:
            prop = crit["remedy"].get("property")
            if prop and crit["remedy"]["route"] != "report-only":
                assert not is_identifier_field(prop), f"{crit['id']} asks for {prop}"

    def test_an_actionable_route_names_a_field_to_write(self, criteria):
        for crit in criteria:
            remedy = crit["remedy"]
            if remedy["route"] != "report-only":
                assert remedy["property"], f"{crit['id']} has nothing to set"

    def test_nothing_claims_to_be_auto_fixable_yet(self, criteria):
        """`auto_fixable` means precisely "fix_required_issues can clear it".

        Claiming it without a matching rule in ``repair._RULES`` would put a gap in
        front of the user that no tool can close.
        """
        assert all(c["remedy"]["route"] != "auto" for c in criteria)


class TestTheGeneratorRefusesToLie:
    def test_an_unknown_criterion_id_raises(self, gen, monkeypatch):
        monkeypatch.setitem(gen.LOCAL_SCOPE, "9.z", ("full", "nope", gen.REPORT_ONLY_REMEDY))
        with pytest.raises(KeyError, match="absent from the published table"):
            gen.build_data()

    def test_a_remedy_naming_an_identifier_field_raises(self, gen, monkeypatch):
        monkeypatch.setitem(
            gen.LOCAL_SCOPE, "2.a", ("full", "descriptive_metadata_rich",
                                     gen.Remedy(None, "doi", "ask-user"))
        )
        with pytest.raises(ValueError, match="identifier"):
            gen.build_data()

    def test_an_actionable_remedy_with_no_field_raises(self, gen, monkeypatch):
        monkeypatch.setitem(
            gen.LOCAL_SCOPE, "2.a", ("full", "descriptive_metadata_rich",
                                     gen.Remedy(None, None, "ask-user"))
        )
        with pytest.raises(ValueError, match="no field"):
            gen.build_data()


class TestTheReport:
    def test_it_states_per_dimension_coverage_and_what_is_out_of_reach(self, gen, data):
        report = gen.format_report(data)
        for name in data["dimensions"].values():
            assert name[:20] in report
        assert "not assessable from a crate" in report
        assert "no aggregate" in report.lower()
