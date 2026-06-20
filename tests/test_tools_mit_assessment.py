"""Tests for builder/tools/mit_assessment.py — assess_mit_coverage tool."""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance, MITReport
from builder.tools.mit_assessment import assess_mit_coverage


class TestAssessMITCoverage:
    """Tests for assess_mit_coverage — compares entity fields against MIT YAML."""

    def test_returns_mit_report(self):
        """assess_mit_coverage returns an MITReport dataclass."""
        state = CrateState()
        result = assess_mit_coverage(state)

        assert isinstance(result, MITReport)

    def test_empty_state_returns_zero_score(self):
        """Empty state returns overall_score of 0.0 and empty module_scores."""
        state = CrateState()
        result = assess_mit_coverage(state)

        assert result.overall_score == 0.0
        assert isinstance(result.module_scores, dict)

    def test_populated_state_has_module_scores(self):
        """State with entities yields per-module scores."""
        state = CrateState()

        # Add a MolecularEntity with some fields filled
        chem = Entity(
            entity_id="chem_001",
            type="MolecularEntity",
            fields={"name": "Test Compound", "smiles": "CCO"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.set_field_status("name", "filled", "llm")
        chem.set_field_status("smiles", "filled", "llm")
        state.add_entity(chem)

        # Add an Investigation with some fields
        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"name": "Test Study", "description": "A test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        inv.set_field_status("name", "filled", "llm")
        inv.set_field_status("description", "filled", "llm")
        state.add_entity(inv)

        result = assess_mit_coverage(state)

        # Should have module scores
        assert len(result.module_scores) > 0
        # Overall score should be > 0 since we have some filled fields
        assert result.overall_score > 0.0

    def test_some_filled_fields_produces_partial_score(self):
        """State with some filled entities yields a partial overall_score < 1.0."""
        state = CrateState()

        # Add a MolecularEntity with some fields filled
        chem = Entity(
            entity_id="chem_001",
            type="MolecularEntity",
            fields={},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.fields["name"] = "Test Compound"
        chem.set_field_status("name", "filled", "llm")
        chem.fields["identifier"] = "CAS-123"
        chem.set_field_status("identifier", "filled", "llm")
        chem.fields["formula"] = "C2H6O"
        chem.set_field_status("formula", "filled", "llm")
        chem.fields["smiles"] = "CCO"
        chem.set_field_status("smiles", "filled", "llm")
        state.add_entity(chem)

        inv = Entity(
            entity_id="inv_001", type="Investigation", fields={},
            _provenance=EntityProvenance(created_by="llm"),
        )
        inv.fields["name"] = "Test Study"
        inv.set_field_status("name", "filled", "llm")
        inv.fields["description"] = "A test"
        inv.set_field_status("description", "filled", "llm")
        state.add_entity(inv)

        result = assess_mit_coverage(state)

        # Score should be > 0 since we have some filled fields
        assert result.overall_score > 0.0
        # But less than 1.0 since many modules have zero coverage
        assert result.overall_score < 1.0

    def test_produces_correct_module_scores(self):
        """Verify module scores structure is correct."""
        state = CrateState()

        # Add a MolecularEntity with 2 fields (Chemical Information module)
        chem = Entity(
            entity_id="chem_001", type="MolecularEntity", fields={},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.fields["name"] = "Test"
        chem.set_field_status("name", "filled", "llm")
        chem.fields["identifier"] = "CAS-123"
        chem.set_field_status("identifier", "filled", "llm")
        state.add_entity(chem)

        result = assess_mit_coverage(state)

        # Should have module scores with the expected structure
        for module_name, scores in result.module_scores.items():
            assert "completed" in scores
            assert "total" in scores
            assert isinstance(scores["completed"], int)
            assert isinstance(scores["total"], int)
            assert scores["completed"] <= scores["total"]