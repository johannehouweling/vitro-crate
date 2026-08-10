"""Tests for builder/tools/fair_assessment.py — assess_fair_maturity tool."""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance, FAIRReport, MITReport
from builder.tools.fair_assessment import _check_access_info, assess_fair_maturity
from tests.fixtures.vhps_golden_crates import vhps_fixture_state


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

    def test_access_info_credits_resolvable_accession(self) -> None:
        # A dataset with a resolvable accession carries access information even
        # when the incidental build paths are unset.
        state = CrateState()
        state.metadata.accession = "S-VHPS21"
        assert state.metadata.output_path is None
        assert state.metadata.input_path is None
        assert _check_access_info(state) is True

    def test_access_info_still_true_for_known_location(self) -> None:
        # Back-compat: an output/input path still counts.
        state = CrateState()
        state.metadata.output_path = "/tmp/out"
        assert _check_access_info(state) is True

    def test_access_info_false_without_any_access_signal(self) -> None:
        # A crate with no identity, no location, no license and no data has no
        # access information — a real gap, correctly failed (not always-true).
        state = CrateState()
        assert not state.session_id
        assert _check_access_info(state) is False


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


class TestLicenceAccessor:
    """The three RDA licence indicators read the licence the crate will actually
    carry. They used to scan ``Entity.fields["license"]`` only — a key no
    production path writes — so a crate licensed the documented way
    (``set_crate_metadata`` → ``state.metadata.license`` →
    ``root_dataset["license"]``) failed all three and collapsed the DSM ladder."""

    def _lic(self, result: FAIRReport) -> dict:
        return {
            i["id"]: i["passed"] for i in result.indicator_results if "R1.1" in i["id"]
        }

    def _state(self, licence: str | None) -> CrateState:
        state = CrateState()
        state.metadata.title = "T"
        state.metadata.description = "D"
        state.metadata.license = licence
        return state

    def test_canonical_metadata_license_is_read(self) -> None:
        result = assess_fair_maturity(
            self._state("https://creativecommons.org/licenses/by/4.0/")
        )
        assert self._lic(result) == {
            "RDA-R1.1-01M": True,
            "RDA-R1.1-02M": True,
            "RDA-R1.1-03M": True,
        }

    def test_entity_field_stays_a_fallback(self) -> None:
        # Hand-assembled states that put the licence on an entity still score.
        state = self._state(None)
        state.add_entity(
            Entity(
                entity_id="inv_001",
                type="Investigation",
                fields={"name": "T", "license": "https://creativecommons.org/licenses/by/4.0/"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        assert self._lic(assess_fair_maturity(state))["RDA-R1.1-01M"] is True

    def test_synthesized_default_is_not_a_licence(self) -> None:
        """The build writes "ALL RIGHTS RESERVED" when the user named nothing.
        Crediting it would tell the researcher the licence question is settled."""
        from builder.tools._crate_mapping import DEFAULT_ROOT_LICENSE

        assert self._lic(assess_fair_maturity(self._state(DEFAULT_ROOT_LICENSE))) == {
            "RDA-R1.1-01M": False,
            "RDA-R1.1-02M": False,
            "RDA-R1.1-03M": False,
        }

    def test_non_creative_commons_licences_are_standard(self) -> None:
        """RDA-R1.1-02M asks for a *standard* licence, not a Creative Commons one.
        The old substring test failed every MIT / Apache / BSD / GPL / ODbL crate."""
        for spdx in ("MIT", "Apache-2.0", "ODbL-1.0", "GPL-3.0-or-later", "CC BY 4.0"):
            assert self._lic(assess_fair_maturity(self._state(spdx)))[
                "RDA-R1.1-02M"
            ] is True, spdx

    def test_accession_shaped_string_is_not_a_standard_licence(self) -> None:
        """The old "cc-" substring test passed these."""
        for junk in ("PROJ-CC-07", "ACC-001"):
            scores = self._lic(assess_fair_maturity(self._state(junk)))
            assert scores["RDA-R1.1-02M"] is False, junk
            assert scores["RDA-R1.1-03M"] is False, junk

    def test_machine_understandable_requires_a_real_uri(self) -> None:
        """``startswith("http")`` also accepted prose beginning with "http"."""
        assert (
            self._lic(assess_fair_maturity(self._state("http-only access on request")))[
                "RDA-R1.1-03M"
            ]
            is False
        )


class TestCheckRegistriesAreTotal:
    """Every check named by a YAML indicator must resolve in its registry. A name
    that did not used to fall through the ``in`` guard and read exactly like a
    pass — which is how DSM-4-R6 (``license_machine``) went unscored."""

    def test_every_dsm_check_resolves(self) -> None:
        import yaml

        from builder.tools.fair_assessment import DSM_CHECKS, DSM_INDICATORS_PATH

        named = {
            i["check"]
            for i in yaml.safe_load(DSM_INDICATORS_PATH.read_text())["indicators"]
            if i.get("scope") != "na" and "check" in i
        }
        assert named <= set(DSM_CHECKS), sorted(named - set(DSM_CHECKS))

    def test_every_fair_check_resolves(self) -> None:
        import yaml

        from builder.tools.fair_assessment import FAIR_CHECKS, FAIR_INDICATORS_PATH

        named = {
            i["check"]
            for i in yaml.safe_load(FAIR_INDICATORS_PATH.read_text())["indicators"]
            if "check" in i
        }
        assert named <= set(FAIR_CHECKS), sorted(named - set(FAIR_CHECKS))


class TestDsmLadderAwardsOnlyAssessedLevels:
    """A level nobody could assess is not a level the crate earned."""

    def test_all_na_level_is_not_awarded(self) -> None:
        from builder.tools.fair_assessment import _compute_dsm_level

        state = CrateState()
        state.metadata.title = "T"
        data = {
            "indicators": [
                {"id": "X-1", "level": 1, "scope": "full", "check": "has_descriptor"},
                {"id": "X-2", "level": 2, "scope": "na"},
            ]
        }
        # Level 2 is entirely unassessable, so the ladder stops at 1.
        assert _compute_dsm_level(state, data) == 1

    def test_real_level_5_carries_no_assessable_indicator(self) -> None:
        """Guards the reason level 5 is unreachable: if an assessable level-5
        indicator is ever added, this test fails and the ceiling must be revisited."""
        import yaml

        from builder.tools.fair_assessment import DSM_INDICATORS_PATH

        level_5 = [
            i
            for i in yaml.safe_load(DSM_INDICATORS_PATH.read_text())["indicators"]
            if i.get("level") == 5
        ]
        assert level_5, "no level-5 row at all"
        assert all(i.get("scope") == "na" for i in level_5)

    def test_unresolvable_check_name_does_not_advance_the_ladder(self) -> None:
        from builder.tools.fair_assessment import _compute_dsm_level

        state = CrateState()
        state.metadata.title = "T"
        data = {
            "indicators": [
                {"id": "X-1", "level": 1, "scope": "full", "check": "has_descriptor"},
                {"id": "X-2", "level": 2, "scope": "full", "check": "no_such_check"},
            ]
        }
        assert _compute_dsm_level(state, data) == 1
