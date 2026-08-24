"""Tests for builder/tools/verification.py."""

from __future__ import annotations

from builder.state import Entity, EntityProvenance, FieldCompletion
from builder.tools import verification
from builder.tools.verification import (
    _VERIFIERS,
    _get_verifiable_fields,
    _select_verifier,
    verify_all_identifiers,
    verify_identifier,
)


def _status(entity: Entity, field: str) -> FieldCompletion:
    """The field's completion record, asserted present.

    `get_field_status` is Optional-typed, so a field that was never tracked
    would otherwise surface as `AttributeError: 'NoneType'` instead of naming
    the field whose status the test expected to exist.
    """
    fc = entity.get_field_status(field)
    assert fc is not None, f"no completion status recorded for {field!r}"
    return fc


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
            lambda query: {
                "found": True,
                "data": {"pubchem_cid": "712"},
                "error": None,
            },
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

    def test_verifies_molecular_entity_dtxsid(self, minimal_state, monkeypatch):
        """MolecularEntity.dtxsid re-resolves via the CompTox lookup (#146)."""
        state = minimal_state
        chem = Entity(
            entity_id="chem_bpa",
            type="MolecularEntity",
            fields={"dtxsid": "DTXSID7020182"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.set_field_status("dtxsid", "filled", "llm")
        state.add_entity(chem)

        seen = {}

        def fake_dtxsid(query):
            seen["query"] = query
            return {
                "found": True,
                "data": {"dtxsid": "DTXSID7020182", "name": "Bisphenol A"},
                "error": None,
            }

        monkeypatch.setattr("builder.tools.verification.lookup_dtxsid", fake_dtxsid)

        result = verify_identifier(state, "chem_bpa", "dtxsid")

        assert result["verified"] is True
        # The stored DTXSID value is what gets re-resolved.
        assert seen["query"] == "DTXSID7020182"
        completion = chem.get_field_status("dtxsid")
        assert completion is not None
        assert completion.status == "verified"
        assert "comptox" in chem._provenance.lookups_used

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
        field_status = person.get_field_status("identifier")
        assert field_status is not None
        assert field_status.status != "missing"


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
        assert "cas_number" in me_fields, "cas_number should be verifiable for MolecularEntity"
        assert "cas" in me_fields, "cas should be verifiable for MolecularEntity"
        assert "inchikey" in me_fields, "inchikey should be verifiable for MolecularEntity"
        assert "identifier" in me_fields, "identifier should be verifiable for MolecularEntity"
        assert "pubchem_cid" in me_fields, "pubchem_cid should be verifiable for MolecularEntity"
        assert "dtxsid" in me_fields, "dtxsid should be verifiable for MolecularEntity"

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
            lambda query: {
                "found": True,
                "data": {"pubchem_cid": "712"},
                "error": None,
            },
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

    def test_every_verifiable_pair_dispatches_to_a_verifier(self):
        """Every pair the authoritative set admits must resolve to a verifier.

        `verify_all_identifiers` decides what to QUEUE from this set, while
        `verify_identifier` decides what to CALL via `_select_verifier`. A pair
        in the set with no verifier is queued and then answered "No verifier
        configured" — and §6 treats a verification failure as REQUIRED, so a
        valid identifier would block the build.
        """
        unwired = sorted(
            (t, f) for (t, f) in _get_verifiable_fields() if _select_verifier(t, f)[0] is None
        )
        assert not unwired, (
            f"verifiable but not dispatchable: {unwired} — "
            "adding a pair to the authoritative set must also wire a verifier"
        )

    def test_that_guard_is_not_vacuous(self, monkeypatch):
        """The guard above must actually fail when the two sources disagree.

        Its predecessor compared `_IDENTIFIER_FIELDS` against the expression it
        was *defined as*, so it could never fail. This injects real drift and
        pins that the check catches it.
        """
        monkeypatch.setattr(
            verification,
            "_VERIFIABLE_FIELDS",
            frozenset(verification._VERIFIABLE_FIELDS | {("Organization", "ror")}),
        )
        unwired = sorted(
            (t, f) for (t, f) in _get_verifiable_fields() if _select_verifier(t, f)[0] is None
        )
        assert unwired == [("Organization", "ror")]

    def test_the_authoritative_set_is_the_dispatch_table(self):
        """One source, not two: the verifiable pairs ARE the table's keys.

        Structural, not conventional — a pair cannot be declared verifiable
        without supplying the verifier that serves it.
        """
        assert _get_verifiable_fields() == frozenset(_VERIFIERS)


class TestCellLineAccessionIdentityCheck:
    """A cell-line accession must resolve to *this* cell line, not merely resolve.

    Regression cover for #383: `lookup_cell_line` answers "is this a real
    Cellosaurus record?", which a transposed-but-real accession passes. Before
    this gate, `CVCL_0027` (HepG2) attached to a CHO-K1 sample was stamped
    `verified`/`lookup` and exported with that provenance.
    """

    @staticmethod
    def _cell_line(name: str, accession: str = "CVCL_0027") -> Entity:
        entity = Entity(
            entity_id="cell_001",
            type="CellLineSample",
            fields={"name": name, "accession": accession},
            _provenance=EntityProvenance(created_by="llm"),
        )
        entity.set_field_status("accession", "filled", "llm")
        return entity

    @staticmethod
    def _resolves_to(monkeypatch, name: str, alternates: list[str] | None = None) -> None:
        data: dict = {"name": name, "identifier": f"https://www.cellosaurus.org/{name}"}
        if alternates:
            data["alternateName"] = alternates
        monkeypatch.setattr(
            "builder.tools.verification.lookup_cell_line",
            lambda _q: {"found": True, "data": data, "error": None},
        )

    def test_transposed_accession_is_not_marked_verified(self, minimal_state, monkeypatch):
        """The issue's exact failure: a real accession for the wrong cell line."""
        state = minimal_state
        state.add_entity(self._cell_line("CHO-K1 hOATP1C1"))
        self._resolves_to(monkeypatch, "HepG2")

        result = verify_identifier(state, "cell_001", "accession")

        assert result["verified"] is False
        assert result["mismatch"] is True
        assert result["resolved_name"] == "HepG2"
        # Both names must appear so the verdict is actionable without a re-lookup.
        assert "HepG2" in result["message"]
        assert "CHO-K1 hOATP1C1" in result["message"]

    def test_transposed_accession_does_not_gain_verified_provenance(
        self, minimal_state, monkeypatch
    ):
        """The D5 violation is the *claim*, not just the return value.

        Asserted on the entity itself: an exported crate reads the field status
        and `lookups_used`, not verify_identifier's dict.
        """
        state = minimal_state
        entity = self._cell_line("CHO-K1 hOATP1C1")
        state.add_entity(entity)
        self._resolves_to(monkeypatch, "HepG2")

        verify_identifier(state, "cell_001", "accession")

        assert _status(entity, "accession").status == "filled"
        assert _status(entity, "accession").source == "llm"
        assert "cellosaurus" not in entity._provenance.lookups_used

    def test_mismatch_keeps_the_value(self, minimal_state, monkeypatch):
        """Non-destructive by design — contrast with the not-found branch.

        An engineered derivative legitimately fails a name match against its
        parent record, so clearing would delete correctly-looked-up accessions.
        """
        state = minimal_state
        entity = self._cell_line("CHO-K1 hOATP1C1")
        state.add_entity(entity)
        self._resolves_to(monkeypatch, "CHO-K1")

        verify_identifier(state, "cell_001", "accession")

        assert entity.fields["accession"] == "CVCL_0027"

    def test_matching_name_still_verifies(self, minimal_state, monkeypatch):
        """The gate must not break the legitimate path it guards."""
        state = minimal_state
        entity = self._cell_line("HepG2")
        state.add_entity(entity)
        self._resolves_to(monkeypatch, "HepG2")

        result = verify_identifier(state, "cell_001", "accession")

        assert result["verified"] is True
        assert _status(entity, "accession").status == "verified"
        assert "cellosaurus" in entity._provenance.lookups_used

    def test_synonym_match_verifies(self, minimal_state, monkeypatch):
        """Cellosaurus records carry synonyms; a synonym is still this record."""
        state = minimal_state
        state.add_entity(self._cell_line("Hep G2"))
        self._resolves_to(monkeypatch, "HepG2", alternates=["Hep-G2", "Hep G2"])

        assert verify_identifier(state, "cell_001", "accession")["verified"] is True

    def test_name_match_ignores_case_and_whitespace(self, minimal_state, monkeypatch):
        """Depositor spreadsheets are not normalized; casing must not be a mismatch."""
        state = minimal_state
        state.add_entity(self._cell_line("  hepg2 "))
        self._resolves_to(monkeypatch, "HepG2")

        assert verify_identifier(state, "cell_001", "accession")["verified"] is True

    def test_unnamed_entity_falls_back_to_existence_only(self, minimal_state, monkeypatch):
        """With no name in hand there is nothing to cross-check against.

        Withholding verification here would be a false negative, not caution.
        """
        state = minimal_state
        entity = Entity(
            entity_id="cell_001",
            type="CellLineSample",
            fields={"accession": "CVCL_0027"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        entity.set_field_status("accession", "filled", "llm")
        state.add_entity(entity)
        self._resolves_to(monkeypatch, "HepG2")

        assert verify_identifier(state, "cell_001", "accession")["verified"] is True

    def test_gate_also_covers_the_identifier_field(self, minimal_state, monkeypatch):
        """Both `accession` and `identifier` route to Cellosaurus (#383 §4)."""
        state = minimal_state
        entity = Entity(
            entity_id="cell_001",
            type="CellLineSample",
            fields={"name": "CHO-K1 hOATP1C1", "identifier": "CVCL_0027"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        entity.set_field_status("identifier", "filled", "llm")
        state.add_entity(entity)
        self._resolves_to(monkeypatch, "HepG2")

        assert verify_identifier(state, "cell_001", "identifier")["mismatch"] is True

    def test_other_entity_types_are_unaffected(self, minimal_state, monkeypatch):
        """The gate is cell-line-specific: a compound name need not equal PubChem's."""
        state = minimal_state
        chem = Entity(
            entity_id="chem_001",
            type="MolecularEntity",
            fields={"name": "T4", "identifier": "51-48-9"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.set_field_status("identifier", "filled", "llm")
        state.add_entity(chem)
        monkeypatch.setattr(
            "builder.tools.verification.lookup_compound",
            lambda _q: {"found": True, "data": {"name": "Levothyroxine"}, "error": None},
        )

        assert verify_identifier(state, "chem_001", "identifier")["verified"] is True
