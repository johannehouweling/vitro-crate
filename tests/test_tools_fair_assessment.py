"""Tests for builder/tools/fair_assessment.py — assess_fair_maturity tool."""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance, FAIRReport, MITReport
from builder.tools.assessment_graph import as_verdict
from builder.tools.fair_assessment import _check_access_info, assess_fair_maturity
from tests.fixtures.vhps_golden_crates import vhps_fixture_state


class TestTheLicenceIndicatorsGradeTheCrate:
    """They read the licence off the assembled graph, not a field nobody writes.

    `_read_declared_licence` (#535) puts the deposit's own licence on
    `state.metadata.license`, and assembly puts it on the Root Data Entity. The four
    licence checks read `entity.fields["license"]` — a field the builder never
    populates and which never reaches the crate. So a crate carrying CC-BY in its
    JSON, and rendering it on the report's study card, scored false on all four.
    """

    @staticmethod
    def _graph(licence: str | None):
        root: dict = {"@id": "./", "@type": "Dataset", "name": "A crate"}
        if licence is not None:
            root["license"] = {"@id": licence}
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "@type": "CreativeWork",
                 "about": {"@id": "./"}},
                root,
            ]
        }

    def _verdicts(self, graph):
        from builder.tools.fair_assessment import assess_fair_maturity

        rep = assess_fair_maturity(CrateState(), graph=graph)
        return {r["id"]: r["passed"] for r in rep.indicator_results}

    def test_a_licence_on_the_crate_is_found(self):
        got = self._verdicts(self._graph("https://creativecommons.org/licenses/by/4.0/"))
        assert got["RDA-R1.1-01M"] is True, "present"
        assert got["RDA-R1.1-02M"] is True, "a standard reuse licence"
        assert got["RDA-R1.1-03M"] is True, "machine-understandable"

    def test_no_licence_on_the_crate_fails_all_three(self):
        got = self._verdicts(self._graph(None))
        assert [got[i] for i in ("RDA-R1.1-01M", "RDA-R1.1-02M", "RDA-R1.1-03M")] == [
            False, False, False,
        ]

    def test_the_not_stated_placeholder_is_not_a_licence(self):
        """`#licence-not-stated` is what assembly writes when nobody declared one.

        Counting it would invert the depositor's own statement in the one direction
        that suppresses reuse — which is the defect #535 was opened for.
        """
        from builder.tools._crate_mapping import LICENCE_NOT_STATED_ID

        got = self._verdicts(self._graph(LICENCE_NOT_STATED_ID))
        assert got["RDA-R1.1-01M"] is False

    def test_a_bare_string_licence_still_counts_as_present(self):
        """"CC-BY" without a version is a declaration, just not a machine-actionable
        one — #535 returns it verbatim rather than inventing a 4.0 URI (D5)."""
        graph = self._graph(None)
        graph["@graph"][1]["license"] = "CC-BY"
        got = self._verdicts(graph)
        assert got["RDA-R1.1-01M"] is True
        assert got["RDA-R1.1-02M"] is True
        assert got["RDA-R1.1-03M"] is False, "no IRI, so not machine-understandable"

    def test_the_dsm_licence_check_is_the_rda_one(self):
        """DSM-3-C7 asks the same question, so it calls the same function.

        Compared at the CHECK level, not through `dsm_verdicts`: the DSM ladder can
        demote a true statement whose lower rungs fail, which is correct behaviour and
        would mask whether the two instruments agree about the licence itself.
        """
        from builder.tools.fair_assessment import (
            DSM_CHECKS,
            FAIR_CHECKS,
            _check_standard_license,
        )

        graph = self._graph("https://creativecommons.org/licenses/by/4.0/")
        assert DSM_CHECKS["standard_license"] is _check_standard_license
        assert _check_standard_license(CrateState(), graph) is True
        assert _check_standard_license(CrateState(), graph) is FAIR_CHECKS[
            "license_standard"
        ](CrateState(), graph)

    def test_a_licence_that_is_really_there_is_reported_as_there(self):
        """DSM-3-C7 is in none of the sheet's nine linked pairs, so nothing rewrites it.

        Pinned as the inverse of what it used to assert. The scorer once demoted this
        to False because a thin crate fails DSM-1-C2 a level below — a constraint the
        published sheet does not impose, and one that reported a declared licence as
        missing.
        """
        from builder.tools.fair_assessment import dsm_verdicts

        graph = self._graph("https://creativecommons.org/licenses/by/4.0/")
        verdict = dsm_verdicts(CrateState(), None, graph)["DSM-3-C7"]
        assert verdict.value is True
        assert verdict.evidence == "", "nothing rewrote it, so it carries no ladder note"

    def test_state_and_graph_give_the_same_answer(self):
        """`#535` writes the licence to `state.metadata.license`; assembly copies it to
        the root. Both are the same fact, so a caller holding either must not get a
        different verdict — that divergence is the whole defect."""
        from builder.tools.fair_assessment import _effective_license

        iri = "https://creativecommons.org/licenses/by/4.0/"
        from_state = CrateState()
        from_state.metadata.license = iri
        assert _effective_license(from_state, None) == iri
        assert _effective_license(CrateState(), self._graph(iri)) == iri

    def test_nothing_anywhere_declares_a_licence(self):
        """With no graph and no state licence the answer is "no", not "unknown" —
        the fact is genuinely absent from both places it could live."""
        from builder.tools.fair_assessment import _effective_license

        assert _effective_license(CrateState(), None) == ''


class TestAssessFairMaturity:
    """Tests for assess_fair_maturity — assesses FAIR maturity from CrateState."""

    def test_returns_fair_report(self):
        """assess_fair_maturity returns a FAIRReport dataclass."""
        state = CrateState()
        result = assess_fair_maturity(state)

        assert isinstance(result, FAIRReport)

    def test_empty_state_returns_default_structure(self):
        """Empty state returns indicator_results list and dsm_level 0."""
        state = CrateState()
        result = assess_fair_maturity(state)

        assert isinstance(result.indicator_results, list)
        assert result.dsm_level == 0

    def test_state_with_metadata_has_indicator_results(self):
        """State with entities and metadata produces indicator results."""
        state = CrateState()
        state.metadata.title = "Test Crate"
        state.metadata.description = "A test crate description"

        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"name": "Test", "description": "Desc"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        inv.set_field_status("name", "filled", "llm")
        inv.set_field_status("description", "filled", "llm")
        state.add_entity(inv)

        result = assess_fair_maturity(state)

        assert len(result.indicator_results) > 0
        assert isinstance(result.dsm_level, int)
        # With some metadata, DSM level should be at least 1
        assert result.dsm_level >= 0

    def test_indicator_results_have_expected_keys(self):
        """Each indicator result has id, dimension, passed, and text."""
        state = CrateState()
        state.metadata.title = "Test"
        state.metadata.description = "Desc"
        state.metadata.accession = "ACC-001"

        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"name": "Test", "description": "Desc", "license": "CC-BY-4.0"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        for f in ["name", "description", "license"]:
            inv.set_field_status(f, "filled", "llm")
        state.add_entity(inv)

        result = assess_fair_maturity(state)

        for indicator in result.indicator_results:
            assert "id" in indicator
            assert "dimension" in indicator
            assert "passed" in indicator
            assert "text" in indicator

    def test_license_present_indicator(self):
        """License presence is detected in FAIR assessment."""
        state = CrateState()

        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={
                "name": "Test",
                "license": "https://creativecommons.org/licenses/by/4.0/",
            },
            _provenance=EntityProvenance(created_by="llm"),
        )
        inv.set_field_status("name", "filled", "llm")
        inv.set_field_status("license", "filled", "llm")
        state.add_entity(inv)

        result = assess_fair_maturity(state)

        # Find the license indicator
        license_indicators = [
            ind
            for ind in result.indicator_results
            if "license" in ind["id"].lower() or ind["id"].endswith("R1.1-01M")
        ]
        if license_indicators:
            for ind in license_indicators:
                if "out_of_scope" not in str(ind.get("scope", "")):
                    assert ind["passed"] is True


class TestDsmLevelNotCollapsed:
    """The DSM ladder must reflect the indicators actually met, not collapse to 0
    because one brittle L1 check reads incidental build-time state (#311).

    ``access_info`` (DSM-1-C3) previously gated only on ``output_path`` /
    ``input_path`` — unset on the report/fixture paths — so a single L1 miss
    zeroed the whole ladder even for a conformant crate. It now credits crate
    content (a resolvable identity, reuse terms, included data, or a known
    location).
    """

    def test_conformant_crate_reaches_non_collapsed_dsm_level(self) -> None:
        # S-VHPS21 passes every assessable L1 + L2 indicator; only L3 has real
        # gaps (resolvable_terms / standard_license / controlled_values). So the
        # honest level is 2 — not 0 collapsed by access_info.
        level = assess_fair_maturity(vhps_fixture_state("S-VHPS21")).dsm_level
        assert level >= 2, f"DSM collapsed to {level}; expected >= 2"

    def test_access_info_reads_the_crate_not_the_session(self) -> None:
        """It used to pass on `state.session_id` — a timestamp this tool mints for its
        own bookkeeping, present on every run and absent from the crate (#706)."""
        state = CrateState()
        state.session_id = "20260101_120000"
        assert _check_access_info(state, None) is None, "no graph, nothing to read"
        empty = {"@graph": [{"@id": "./", "@type": "Dataset"}]}
        assert as_verdict(_check_access_info(state, empty)).value is False

    def test_access_info_credits_a_descriptor_that_says_how_to_reach_the_data(self) -> None:
        for extra, why in (
            ({"identifier": "https://doi.org/10.6019/S-VHPS22"}, "a resolvable identifier"),
            ({"license": {"@id": "https://creativecommons.org/licenses/by/4.0/"}}, "reuse terms"),
            ({"conditionsOfAccess": "public"}, "stated access conditions"),
        ):
            graph = {"@graph": [{"@id": "./", "@type": "Dataset", **extra}]}
            assert as_verdict(_check_access_info(CrateState(), graph)).value is True, why

    def test_access_info_credits_data_the_descriptor_actually_lists(self) -> None:
        graph = {
            "@graph": [
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "data/plate1.csv"}]},
                {"@id": "data/plate1.csv", "@type": "File", "encodingFormat": "text/csv"},
            ]
        }
        verdict = as_verdict(_check_access_info(CrateState(), graph))
        assert verdict.value is True
        assert "deposited file" in verdict.evidence


class TestMitCoverageIndicatorCoupling:
    """The FAIR RDA-R1.3-01D (``mit_coverage``) indicator read the never-populated
    ``state.mit_assessment`` on the report/export path, so it could never pass even
    when MIT coverage was fine. It now honours a MIT report passed by the caller
    (which scores against the assembled @graph), falling back to
    ``state.mit_assessment`` for back-compat (#311)."""

    def _mit_indicator(self, result: FAIRReport) -> dict:
        ind = next(
            (i for i in result.indicator_results if i.get("id") == "RDA-R1.3-01D"), None
        )
        assert ind is not None, "mit_coverage indicator (RDA-R1.3-01D) not found"
        return ind

    def test_indicator_uses_passed_mit_report(self) -> None:
        state = CrateState()
        state.metadata.title = "T"
        mit = MITReport(module_scores={"m": {"completed": 1, "total": 2}}, overall_score=0.5)
        # state.mit_assessment stays the empty default; the passed report wins.
        result = assess_fair_maturity(state, mit=mit)
        assert self._mit_indicator(result)["passed"] is True

    def test_indicator_falls_back_to_state_when_no_mit_passed(self) -> None:
        # No mit passed and the default empty state.mit_assessment → not covered.
        state = CrateState()
        state.metadata.title = "T"
        result = assess_fair_maturity(state)
        assert self._mit_indicator(result)["passed"] is False


class TestDsmBlockers:
    """#607: the report says "N indicators to level L" and names them, so the
    blockers must be exactly the *next* level's failing assessable indicators.

    Scored against a synthetic indicator table so the expectation is the
    table, not the function under test."""

    _TABLE = {
        "indicators": [
            {"id": "L1-PASS", "level": 1, "scope": "full", "check": "unique_id",
             "text": "level 1, passes"},
            {"id": "L2-FAIL-A", "level": 2, "scope": "full", "check": "fails_a",
             "text": "level 2, fails A"},
            {"id": "L2-FAIL-B", "level": 2, "scope": "partial", "check": "fails_b",
             "text": "level 2, fails B"},
            {"id": "L2-NA", "level": 2, "scope": "na", "check": "fails_c",
             "text": "level 2, not assessable"},
            {"id": "L3-FAIL", "level": 3, "scope": "full", "check": "fails_d",
             "text": "level 3, fails"},
        ]
    }

    def _patched(self, monkeypatch):
        import builder.tools.fair_assessment as fa

        monkeypatch.setattr(fa, "_load_yaml", lambda path: dict(self._TABLE))
        monkeypatch.setitem(fa.DSM_CHECKS, "unique_id", lambda state, graph=None: True)
        for name in ("fails_a", "fails_b", "fails_c", "fails_d"):
            monkeypatch.setitem(fa.DSM_CHECKS, name, lambda state, graph=None: False)
        return fa

    def test_only_the_next_levels_assessable_failures_block(self, monkeypatch):
        fa = self._patched(monkeypatch)
        state = CrateState()
        assert fa._compute_dsm_level(state, dict(self._TABLE)) == 1
        assert fa.dsm_ceiling(state)["blocked_by"] == [
            ("L2-FAIL-A", "level 2, fails A", ""),
            ("L2-FAIL-B", "level 2, fails B", ""),
        ], "na scope excluded, level 3 not yet in play, ids paired with their text"

    def test_a_level_five_crate_has_nothing_above_to_block(self, monkeypatch):
        import builder.tools.fair_assessment as fa

        monkeypatch.setattr(
            fa,
            "_load_yaml",
            lambda path: {
                "indicators": [
                    {"id": f"L{lvl}", "level": lvl, "scope": "full", "check": "ok", "text": "t"}
                    for lvl in range(1, 6)
                ]
            },
        )
        monkeypatch.setitem(fa.DSM_CHECKS, "ok", lambda state, graph=None: True)
        state = CrateState()
        assert fa.dsm_ceiling(state)["blocked_by"] == []

    def test_an_unreadable_table_blocks_nothing(self, monkeypatch):
        import builder.tools.fair_assessment as fa

        monkeypatch.setattr(fa, "_load_yaml", lambda path: None)
        assert fa.dsm_ceiling(CrateState())["blocked_by"] == []

    def test_the_shipped_table_blocks_what_the_level_computation_uses(self):
        """Against the real YAML: every blocker is a level+1 indicator whose
        own check fails, and clearing them is what the level gate asks for."""
        from builder.tools.fair_assessment import (
            DSM_CHECKS,
            DSM_INDICATORS_PATH,
            _compute_dsm_level,
            _load_yaml,
            dsm_ceiling,
        )

        state = vhps_fixture_state("S-VHPS21")
        data = _load_yaml(DSM_INDICATORS_PATH)
        assert data is not None
        level = _compute_dsm_level(state, data)
        by_id = {i["id"]: i for i in data["indicators"]}
        blockers = dsm_ceiling(state)["blocked_by"]
        assert blockers, "the fixture must have blockers for this to test anything"
        for bid, text, _why in blockers:
            ind = by_id[bid]
            assert ind["level"] == level + 1
            assert ind["scope"] != "na"
            assert text == ind["text"]
            assert DSM_CHECKS[ind["check"]](state, None) is False


class TestTheAgentAndTheReportScoreTheSameCrate:
    """One crate, one FAIR number — whoever asks.

    The tool spec exposes no parameters, so a model reaches ``assess_fair_maturity``
    with nothing but the state; ``_assess_fair_maturity_tool`` assembles the crate at
    that boundary so the graph-aware indicators can answer. That fixed the graph and
    left the second argument: the report also computes MIT against the same graph and
    feeds it back in, because ``state.mit_assessment`` is never populated on either
    path. Miss it and RDA-R1.3-01D is False for the agent and True on the page, for the
    same bytes (#713).

    Asserting the whole result rather than that one indicator is the point: the two
    call sites drifted because nothing compared them, and a third argument would drift
    the same way.
    """

    def _both_paths(self) -> tuple[FAIRReport, FAIRReport]:
        from builder.tools.fair_assessment import _assess_fair_maturity_tool
        from builder.tools.mit_assessment import assess_mit_coverage, scoring_graph

        state = vhps_fixture_state("S-VHPS21")
        graph = scoring_graph(state)
        # What build_maturity_html does (maturity_report.py), spelled out.
        report = assess_fair_maturity(
            state, mit=assess_mit_coverage(state, graph=graph), graph=graph
        )
        return _assess_fair_maturity_tool(state), report

    def test_every_indicator_agrees(self) -> None:
        agent, report = self._both_paths()
        assert {i["id"]: i["passed"] for i in agent.indicator_results} == {
            i["id"]: i["passed"] for i in report.indicator_results
        }

    def test_the_dsm_level_agrees(self) -> None:
        agent, report = self._both_paths()
        assert agent.dsm_level == report.dsm_level


class TestTheTwoF1IndicatorsAskTwoQuestions:
    """RDA-F1-01M and RDA-F1-02M are different questions about different actors.

    The published model (10.15497/rda00050 §4.2) separates them: *persistent* is a
    promise by the issuing organisation that the identifier will keep resolving;
    *globally unique* is a structural property of the namespace it was minted in, so
    no other issuer could ever produce the same string. Neither implies the other — a
    UUID is globally unique and nobody has promised anything about it — and the model
    scores both Essential.

    The old `root_global_id` check was `bool(state.metadata.accession or
    state.session_id)`, true after every run that got as far as being scored (#712).
    The obvious repair — point it at `_root_pid` like RDA-F1-01M — would answer the
    persistence question twice under two published ids, which is why the two
    predicates are pinned here by the case that separates them.
    """

    @staticmethod
    def _graph(identifier: str | None) -> dict:
        root: dict = {"@id": "./", "@type": "Dataset", "name": "A crate"}
        if identifier is not None:
            root["identifier"] = identifier
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "@type": "CreativeWork",
                 "about": {"@id": "./"}},
                root,
            ]
        }

    def _f1(self, identifier: str | None) -> tuple:
        """(persistent, globally unique) for a root carrying *identifier*."""
        state = CrateState()
        state.session_id = "20260101_000000"  # the handle the old check passed on
        rep = assess_fair_maturity(state, graph=self._graph(identifier))
        got = {r["id"]: r["passed"] for r in rep.indicator_results}
        return got["RDA-F1-01M"], got["RDA-F1-02M"]

    def test_a_repository_url_is_unique_without_being_persistent(self) -> None:
        """The case that keeps the two indicators from collapsing into one, and the
        crate the tool's own remedy proposes: a landing page in a DNS-partitioned
        namespace, issued by a body that has promised nothing about keeping it."""
        assert self._f1("https://www.ebi.ac.uk/biostudies/studies/S-VHPS22") == (False, True)

    def test_a_doi_is_both(self) -> None:
        assert self._f1("https://doi.org/10.6019/S-VHPS22") == (True, True)

    def test_a_bare_accession_is_neither(self) -> None:
        """Unique inside BioStudies, ambiguous outside it."""
        assert self._f1("S-VHPS22") == (False, False)

    def test_a_uuid_is_unique_with_no_promise_attached(self) -> None:
        assert self._f1("urn:uuid:8f1e2a3b-4c5d-6e7f-8091-a2b3c4d5e6f7") == (False, True)

    def test_a_national_library_urn_is_persistent(self) -> None:
        assert self._f1("urn:nbn:nl:ui:13-abcdef") == (True, True)

    def test_the_substring_doi_is_not_a_scheme(self) -> None:
        """Measured false positives of the old `_root_pid`: it accepted anything
        containing "doi" and anything starting "10.", so a note to self scored as a
        persistent identifier."""
        assert self._f1("my_doi_notes") == (False, False)
        assert self._f1("10.happy") == (False, False)

    def test_a_session_handle_carries_neither(self) -> None:
        """No reader ever receives `session_id`; a crate with no identifier at all
        must fail both however the run was launched."""
        assert self._f1(None) == (False, False)

    def test_no_graph_is_not_assessed(self) -> None:
        state = CrateState()
        state.session_id = "20260101_000000"
        rep = assess_fair_maturity(state)
        got = {r["id"]: r["passed"] for r in rep.indicator_results}
        assert got["RDA-F1-01M"] is None and got["RDA-F1-02M"] is None
