"""Tests for builder/tools/verification.py."""

from __future__ import annotations

import pytest

from builder.state import Entity, EntityProvenance
from builder.tools.verification import (
    _IDENTIFIER_FIELDS,
    _get_verifiable_fields,
    verify_all_identifiers,
    verify_identifier,
)


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

    def test_transient_failure_keeps_value(self, minimal_state, monkeypatch):
        """A transient lookup failure must NOT delete the user's value."""
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
            lambda query: {
                "found": False,
                "data": {},
                "error": "PubChem temporarily unavailable (transient): timeout",
                "transient": True,
            },
        )

        result = verify_identifier(state, "chem_001", "identifier")

        assert result["verified"] is False
        # Value is preserved (NOT cleared) on a transient failure.
        assert chem.fields["identifier"] == "50-00-0"
        status = chem.get_field_status("identifier")
        assert status is not None
        assert status.status != "missing"

    def test_transient_orcid_not_verified_and_kept(self, minimal_state, monkeypatch):
        """A transient ORCID error is neither verified nor cleared (no false +)."""
        state = minimal_state
        person = Entity(
            entity_id="p_001",
            type="Person",
            fields={"identifier": "0000-0001-6004-8653"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        person.set_field_status("identifier", "filled", "llm")
        state.add_entity(person)

        monkeypatch.setattr(
            "builder.tools.verification.lookup_orcid",
            lambda query: {
                "found": False,
                "data": {},
                "error": "ORCID temporarily unavailable (transient): timeout",
                "transient": True,
            },
        )

        result = verify_identifier(state, "p_001", "identifier")

        assert result["verified"] is False
        assert person.fields["identifier"] == "0000-0001-6004-8653"
        assert person.get_field_status("identifier").status != "missing"


class TestVerifyAllIdentifiers:
    """Tests for the verify_all_identifiers function."""

    def test_runs_across_all_entities(self, state_with_multiple_entities):
        """verify_all_identifiers returns results only for (entity_type, field) pairs
        that have an actual verifier configured, and skips non-identifier fields as
        well as identifier fields on entity types without a verifier."""
        state = state_with_multiple_entities

        # Mark verifiable and non-verifiable fields as filled
        # Investigation has no verifier, so its identifier field won't be attempted
        inv = state.get_entity("inv_001")
        inv.set_field_status("identifier", "filled", "user")
        inv.set_field_status("title", "filled", "llm")  # should be skipped

        # MolecularEntity has a PubChem verifier — identifier and casrn are verifiable
        chem = state.get_entity("chem_001")
        chem.set_field_status("identifier", "filled", "llm")
        chem.set_field_status("pubchem_cid", "filled", "llm")
        chem.set_field_status("name", "filled", "llm")  # should be skipped

        results = verify_all_identifiers(state)

        # Only (MolecularEntity, identifier) and (MolecularEntity, pubchem_cid) should
        # produce results; Investigation fields have no verifier so they are skipped.
        assert len(results) == 2

        entity_fields = {(r["entity_id"], r["field"]) for r in results}
        assert ("chem_001", "identifier") in entity_fields
        assert ("chem_001", "pubchem_cid") in entity_fields
        # Investigation has no verifier — fields are skipped
        assert ("inv_001", "identifier") not in entity_fields
        # Non-identifier fields are skipped
        assert ("inv_001", "title") not in entity_fields
        assert ("chem_001", "name") not in entity_fields

        # All results should have the expected structure
        for r in results:
            assert "verified" in r
            assert "message" in r
            assert "suggested_fix" in r


class TestVerifiableFieldSet:
    """Tests that the verifiable field set is derived from _select_verifier."""

    def test_molecular_entity_cas_fields_are_verifiable(self):
        """casrn, cas_number, cas, and inchikey on MolecularEntity are included."""
        vf = _get_verifiable_fields()
        me_fields = {f for (t, f) in vf if t == "MolecularEntity"}
        assert "casrn" in me_fields, "casrn should be verifiable for MolecularEntity"
        assert "cas_number" in me_fields, (
            "cas_number should be verifiable for MolecularEntity"
        )
        assert "cas" in me_fields, "cas should be verifiable for MolecularEntity"
        assert "inchikey" in me_fields, (
            "inchikey should be verifiable for MolecularEntity"
        )
        assert "identifier" in me_fields, (
            "identifier should be verifiable for MolecularEntity"
        )
        assert "pubchem_cid" in me_fields, (
            "pubchem_cid should be verifiable for MolecularEntity"
        )

    def test_organization_ror_not_verifiable(self):
        """Organization has no verifier, so ror should not be in the set."""
        vf = _get_verifiable_fields()
        org_ror = ("Organization", "ror")
        assert org_ror not in vf, (
            "Organization ror should NOT be verifiable since no verifier exists"
        )

    def test_verifiable_fields_include_cell_line_fields(self):
        """CellLineSample identifier and accession should be verifiable."""
        vf = _get_verifiable_fields()
        cl_fields = {f for (t, f) in vf if t == "CellLineSample"}
        assert "identifier" in cl_fields
        assert "accession" in cl_fields

    def test_verifiable_fields_include_person_fields(self):
        """Person identifier and orcid should be verifiable."""
        vf = _get_verifiable_fields()
        p_fields = {f for (t, f) in vf if t == "Person"}
        assert "identifier" in p_fields
        assert "orcid" in p_fields

    def test_verifiable_fields_include_publication_fields(self):
        """Publication identifier and doi should be verifiable."""
        vf = _get_verifiable_fields()
        pub_fields = {f for (t, f) in vf if t == "Publication"}
        assert "identifier" in pub_fields
        assert "doi" in pub_fields

    def test_verify_all_identifiers_catches_casrn_and_inchikey(self, monkeypatch):
        """verify_all_identifiers picks up casrn and inchikey as filled fields
        on MolecularEntity and attempts verification."""
        from builder.state import CrateState

        state = CrateState()
        chem = Entity(
            entity_id="chem_001",
            type="MolecularEntity",
            fields={"casrn": "50-00-0", "inchikey": "WSFSSNUMVMOOMR-UHFFFAOYSA-N"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.set_field_status("casrn", "filled", "llm")
        chem.set_field_status("inchikey", "filled", "llm")
        state.add_entity(chem)

        monkeypatch.setattr(
            "builder.tools.verification.lookup_compound",
            lambda query: {"found": True, "data": {"pubchem_cid": "712"}, "error": None},
        )

        results = verify_all_identifiers(state)

        result_fields = {(r["entity_id"], r["field"]) for r in results}
        assert ("chem_001", "casrn") in result_fields, (
            "casrn should be picked up by verify_all_identifiers"
        )
        assert ("chem_001", "inchikey") in result_fields, (
            "inchikey should be picked up by verify_all_identifiers"
        )

    def test_organization_ror_not_in_verify_all_identifiers_results(self):
        """Organization ror (with no verifier) produces no result from
        verify_all_identifiers — no misleading 'No verifier configured' entry."""
        from builder.state import CrateState

        state = CrateState()
        org = Entity(
            entity_id="org_001",
            type="Organization",
            fields={"ror": "https://ror.org/123456"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        org.set_field_status("ror", "filled", "llm")
        state.add_entity(org)

        results = verify_all_identifiers(state)

        ror_results = [r for r in results if r["field"] == "ror"]
        assert len(ror_results) == 0, (
            "Organization ror should produce NO results from verify_all_identifiers"
        )

    def test_sync_test_fails_if_drift_occurs(self):
        """Ensure _IDENTIFIER_FIELDS (legacy set) and _get_verifiable_fields()
        stay in sync — this test will fail if they drift apart, alerting
        developers to update both."""
        # Get the flat set of field names from _get_verifiable_fields
        derived_fields = {f for (_t, f) in _get_verifiable_fields()}

        # Every field in _IDENTIFIER_FIELDS that has a verifier should also
        # appear in the derived set. (Fields like 'ror' that have NO verifier
        # are excluded from the derived set.)
        for field in _IDENTIFIER_FIELDS:
            if field == "ror":
                continue  # ror has no verifier; allowed to be missing
            assert field in derived_fields, (
                f"{field} is in _IDENTIFIER_FIELDS but NOT in derived "
                f"verifiable fields — they must be kept in sync"
            )