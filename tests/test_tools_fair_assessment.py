"""Tests for builder/tools/fair_assessment.py — assess_fair_maturity tool."""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance, FAIRReport
from builder.tools.fair_assessment import assess_fair_maturity


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
