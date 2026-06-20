"""Tests for builder/tools/verification.py."""

from __future__ import annotations

import pytest

from builder.state import Entity, EntityProvenance
from builder.tools.verification import verify_all_identifiers, verify_identifier


class TestVerifyIdentifier:
    """Tests for the verify_identifier function."""

    def test_returns_expected_structure_for_known_entity(self, minimal_state):
        """verify_identifier returns the expected dict shape for a known entity
        with a filled field."""
        state = minimal_state
        entity = state.get_entity("inv_001")
        entity.set_field_status("title", "filled", "llm")

        result = verify_identifier(state, "inv_001", "title")

        assert isinstance(result, dict)
        assert "verified" in result
        assert "entity_id" in result
        assert "field" in result
        assert "message" in result
        assert "suggested_fix" in result

        assert result["entity_id"] == "inv_001"
        assert result["field"] == "title"

    def test_returns_expected_structure_for_nonexistent_entity(self, minimal_state):
        """verify_identifier returns a result dict even for non-existent entities."""
        result = verify_identifier(minimal_state, "does_not_exist", "title")

        assert isinstance(result, dict)
        assert result["verified"] is False
        assert result["entity_id"] == "does_not_exist"
        assert result["field"] == "title"
        assert "not found" in result["message"].lower()
        assert result["suggested_fix"] is not None

    def test_verifies_supported_identifier_field(self, minimal_state, monkeypatch):
        """verify_identifier verifies known identifiers via lookup tools."""
        state = minimal_state
        chem = Entity(
            entity_id="chem_001",
            type="MolecularEntity",
            fields={"identifier": "50-00-0"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.set_field_status("identifier", "filled", "llm")
        state.add_entity(chem)

        monkeypatch.setattr(
            "builder.tools.verification.lookup_compound",
            lambda query: {"found": True, "data": {"pubchem_cid": "712"}, "error": None},
        )

        result = verify_identifier(state, "chem_001", "identifier")

        assert result["verified"] is True
        completion = chem.get_field_status("identifier")
        assert completion is not None
        assert completion.status == "verified"
        assert "pubchem" in chem._provenance.lookups_used

    def test_clears_identifier_when_verification_fails(self, minimal_state, monkeypatch):
        """verify_identifier clears unresolved identifier values."""
        state = minimal_state
        chem = Entity(
            entity_id="chem_001",
            type="MolecularEntity",
            fields={"identifier": "not-real"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.set_field_status("identifier", "filled", "llm")
        state.add_entity(chem)

        monkeypatch.setattr(
            "builder.tools.verification.lookup_compound",
            lambda query: {"found": False, "data": {}, "error": "not found"},
        )

        result = verify_identifier(state, "chem_001", "identifier")

        assert result["verified"] is False
        assert "identifier" not in chem.fields
        completion2 = chem.get_field_status("identifier")
        assert completion2 is not None
        assert completion2.status == "missing"


class TestVerifyAllIdentifiers:
    """Tests for the verify_all_identifiers function."""

    def test_runs_across_all_entities(self, state_with_multiple_entities):
        """verify_all_identifiers returns one result per identifier field that is 'filled',
        and skips non-identifier fields such as title and description."""
        state = state_with_multiple_entities

        # Mark identifier-like and non-identifier fields as filled
        inv = state.get_entity("inv_001")
        inv.set_field_status("identifier", "filled", "user")
        inv.set_field_status("doi", "filled", "llm")
        inv.set_field_status("title", "filled", "llm")  # should be skipped

        study = state.get_entity("stu_001")
        study.set_field_status("accession", "filled", "llm")
        study.set_field_status("description", "filled", "llm")  # should be skipped

        results = verify_all_identifiers(state)

        # Only identifier-like fields (identifier, doi, accession) should produce results
        assert len(results) == 3

        entity_fields = {(r["entity_id"], r["field"]) for r in results}
        assert ("inv_001", "identifier") in entity_fields
        assert ("inv_001", "doi") in entity_fields
        assert ("stu_001", "accession") in entity_fields
        assert ("inv_001", "title") not in entity_fields
        assert ("stu_001", "description") not in entity_fields

        # All results should have the expected structure
        for r in results:
            assert "verified" in r
            assert "message" in r
            assert "suggested_fix" in r