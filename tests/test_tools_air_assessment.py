"""Scoring a crate against the published Bridge2AI AI-readiness criteria.

The instrument's own rules, which these tests hold the implementation to:

* **Seven percentages, no aggregate.** The authors state it outright — *"We do not
  score it pass/fail overall"*. A single AI-readiness number would be our invention.
* **Their denominator counts everything; ours can drop a criterion.** Both are
  reported, so the figure the authors' own worksheet would produce is never replaced
  by ours without saying so.
* **A criterion a crate cannot evidence is never failed.** Ethics, governance and
  hosting are `na` — reported, excluded from our denominator, counted as unmet in
  theirs.
"""

from __future__ import annotations

import pytest

from builder.state import AIRReport, CrateState, Entity, EntityProvenance
from builder.tools.air_assessment import (
    AIR_CHECKS,
    air_blockers,
    air_profile,
    air_verdicts,
    assess_air_readiness,
)
from builder.tools.assessment_graph import Verdict


def _graph(*nodes: dict) -> list[dict]:
    """A minimal assembled crate: the descriptor, the root, then whatever is asked."""
    return [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
        {"@id": "./", "@type": "Dataset", "name": "A crate"},
        *nodes,
    ]


class TestNoGraphMeansUnanswered:
    """Absent evidence is not evidence of absence."""

    def test_graph_checks_return_none_rather_than_false(self):
        state = CrateState()
        verdicts = air_verdicts(state, None, None)
        graph_aware = [v for v in verdicts.values() if v.value is None]
        assert graph_aware, "every criterion answered without a graph — one of them is lying"

    def test_an_unassessable_criterion_is_never_failed(self):
        """4.a asks about IRB and consent. A crate cannot show either, ever."""
        verdicts = air_verdicts(CrateState(), None, _graph())
        assert verdicts["4.a"].value is None
        assert verdicts["4.b"].value is None


class TestTheProfileFollowsThePublishedFormula:
    def test_it_reports_seven_dimensions(self):
        profile = air_profile(CrateState(), None, _graph())
        assert [d["dimension"] for d in profile] == [0, 1, 2, 3, 4, 5, 6]

    def test_there_is_no_aggregate_score_anywhere(self):
        """The one number the authors refuse to compute."""
        report = assess_air_readiness(CrateState(), graph=_graph())
        flat = report.to_dict()
        assert not any(
            key in flat for key in ("score", "overall", "overall_score", "air_score", "total")
        ), "an aggregate AI-readiness score is exactly what this axis exists to stop"

    def test_our_denominator_excludes_what_was_not_assessed(self):
        profile = {d["dimension"]: d for d in air_profile(CrateState(), None, _graph())}
        ethics = profile[4]
        assert ethics["total"] == 4, "the published dimension size never changes"
        assert ethics["assessed"] < ethics["total"], "3 of the 4 are unanswerable"

    def test_the_publishers_own_number_is_reported_beside_ours(self):
        """Their COUNTIF denominator is every criterion — unassessed counts as unmet."""
        profile = {d["dimension"]: d for d in air_profile(CrateState(), None, _graph())}
        ethics = profile[4]
        assert ethics["published_pct"] == pytest.approx(ethics["met"] / 4 * 100)
        if ethics["assessed"]:
            assert ethics["pct"] == pytest.approx(ethics["met"] / ethics["assessed"] * 100)

    def test_a_dimension_with_nothing_assessed_reports_none_not_zero(self):
        """`pct=None` is "we did not look"; `0.0` is "the crate failed". Different claims."""
        from builder.tools import air_assessment as air

        data = {
            "dimensions": {0: "Solo"},
            "criteria": [
                {"id": "0.a", "dimension": 0, "scope": "na", "label": "x", "text": "x"},
            ],
        }
        profile = air.air_profile(CrateState(), data, _graph())
        assert profile[0]["pct"] is None
        assert profile[0]["published_pct"] == 0.0, "theirs still counts it as unmet"


class TestEveryVerdictCarriesItsEvidence:
    def test_evidence_quantifies_what_was_found(self):
        graph = _graph(
            {"@id": "#p1", "@type": "LabProcess", "object": {"@id": "#s1"}},
            {"@id": "#p2", "@type": "LabProcess"},
        )
        verdicts = air_verdicts(CrateState(), None, graph)
        measured = [v for v in verdicts.values() if v.evidence]
        assert measured, "no verdict carried evidence"
        joined = " ".join(v.evidence for v in measured)
        assert any(ch.isdigit() for ch in joined), "evidence should quantify, not restate"

    def test_blockers_name_the_criterion_and_why(self):
        blockers = air_blockers(CrateState(), _graph())
        assert blockers, "an empty crate fails something"
        ident, text, evidence = blockers[0]
        assert "." in ident, "a criterion id, e.g. 1.b"
        assert text, "the published practice sentence"
        assert isinstance(evidence, str)

    def test_verdict_refuses_truthiness(self):
        with pytest.raises(TypeError, match="tri-state"):
            bool(Verdict(None, ""))


class TestTheChecksMeasureWhatTheCriterionAsks:
    def test_wired_processes_satisfy_traceability(self):
        wired = _graph(
            {"@id": "#p1", "@type": "LabProcess", "object": {"@id": "#s1"},
             "result": {"@id": "#f1"}},
        )
        assert air_verdicts(CrateState(), None, wired)["1.b"].value is True

    def test_unwired_processes_fail_traceability(self):
        bare = _graph({"@id": "#p1", "@type": "LabProcess", "name": "Exposure"})
        verdict = air_verdicts(CrateState(), None, bare)["1.b"]
        assert verdict.value is False
        assert "0" in verdict.evidence or "no" in verdict.evidence.lower()

    def test_an_orcid_and_a_ror_identify_the_key_actors(self):
        graph = _graph(
            {"@id": "https://orcid.org/0000-0002-1825-0097", "@type": "Person", "name": "A"},
            {"@id": "https://ror.org/01cesdt21", "@type": "Organization", "name": "RIVM"},
        )
        assert air_verdicts(CrateState(), None, graph)["1.d"].value is True

    def test_a_bare_person_node_does_not(self):
        graph = _graph({"@id": "#person-a", "@type": "Person", "name": "A"})
        assert air_verdicts(CrateState(), None, graph)["1.d"].value is False

    def test_a_file_without_a_checksum_fails_verifiability(self):
        graph = _graph({"@id": "data.csv", "@type": "File", "encodingFormat": "text/csv"})
        assert air_verdicts(CrateState(), None, graph)["3.c"].value is False

    def test_a_hashed_file_satisfies_it(self):
        graph = _graph({"@id": "data.csv", "@type": "File", "sha256": "ab" * 32})
        assert air_verdicts(CrateState(), None, graph)["3.c"].value is True

    def test_software_needs_a_repository_not_just_a_name(self):
        named = _graph({"@id": "#sw", "@type": "SoftwareApplication", "name": "DESeq2"})
        assert air_verdicts(CrateState(), None, named)["1.c"].value is False
        hosted = _graph(
            {"@id": "#sw", "@type": "SoftwareSourceCode", "name": "pipeline",
             "codeRepository": "https://github.com/example/pipeline"},
        )
        assert air_verdicts(CrateState(), None, hosted)["1.c"].value is True


class TestSharedQuestionsShareTheirImplementation:
    """Where AIR asks what DSM or RDA already asks, the same function answers.

    Two implementations of one question is how two axes come to disagree about one
    crate — and the manuscript cannot then present them as independent evidence.
    """

    def test_the_licence_criterion_is_the_rda_licence_check(self):
        from builder.tools.fair_assessment import FAIR_CHECKS

        state = CrateState()
        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"name": "T", "license": "https://creativecommons.org/licenses/by/4.0/"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(inv)
        state.metadata.license = "https://creativecommons.org/licenses/by/4.0/"
        assert (
            air_verdicts(state, None, _graph())["0.d"].value
            is FAIR_CHECKS["license_present"](state)
        )

    def test_the_ethics_criterion_does_not_borrow_the_dsm_access_check(self):
        """DSM's `access_info` credits a crate for having a location, an identity, a
        licence, or any data at all. Reusing it here would have made the one Ethics
        criterion a crate can evidence read 100% for every crate ever built."""
        from builder.tools.fair_assessment import _check_access_info

        state = CrateState()
        state.metadata.accession = "S-VHPS26"
        assert _check_access_info(state) is True, "the DSM check passes on identity alone"
        assert air_verdicts(state, None, _graph())["4.d"].value is False

    def test_a_stated_access_condition_satisfies_it(self):
        graph = _graph()
        graph[1]["conditionsOfAccess"] = "public"
        assert air_verdicts(CrateState(), None, graph)["4.d"].value is True

    def test_the_portability_criterion_is_the_dsm_format_check(self):
        from builder.tools.fair_assessment import DSM_CHECKS

        graph = _graph({"@id": "a.csv", "@type": "File", "encodingFormat": "text/csv"})
        state = CrateState()
        mine = air_verdicts(state, None, graph)["6.c"].value
        theirs = DSM_CHECKS["non_proprietary_format"](state, graph)
        assert mine is (theirs.value if isinstance(theirs, Verdict) else theirs)


class TestCriterion6dReadsTheFileClassification:
    """6.d asks for the data components, and any File at all is not that.

    Inherited verbatim from the reproducibility checklist this axis replaced — the
    one predicate of that checklist worth keeping. It used to be
    ``bool(state.list_entities("File"))``, which a crate holding three protocols and
    no measurements satisfied; it counts the files classified as data (#591), and
    reaches that class through the same classifier the rest of the build uses,
    because ``File.role`` is free text that predates the classification and outlives
    it.
    """

    @staticmethod
    def _row(*files: dict[str, str]) -> bool | None:
        state = CrateState()
        for index, fields in enumerate(files):
            state.add_entity(Entity(entity_id=f"file_{index}", type="File", fields=dict(fields)))
        return air_verdicts(state, None, _graph())["6.d"].value

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


class TestTheReportShape:
    def test_it_returns_an_air_report(self):
        assert isinstance(assess_air_readiness(CrateState(), graph=_graph()), AIRReport)

    def test_it_survives_a_save_and_resume(self):
        report = assess_air_readiness(CrateState(), graph=_graph())
        assert AIRReport.from_dict(report.to_dict()) == report

    def test_every_criterion_result_carries_its_published_text(self):
        report = assess_air_readiness(CrateState(), graph=_graph())
        assert len(report.criterion_results) == 28
        for result in report.criterion_results:
            assert result["text"], f"{result['id']} lost its practice sentence"
            assert result["passed"] in (True, False, None)

    def test_every_registered_check_is_used_by_a_criterion(self):
        report = assess_air_readiness(CrateState(), graph=_graph())
        used = {r["check"] for r in report.criterion_results if r.get("check")}
        assert used == set(AIR_CHECKS), "a check nobody calls is a check nobody maintains"
