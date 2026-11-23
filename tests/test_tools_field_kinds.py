"""Tests for builder/tools/field_kinds.py — the shared field-kind vocabulary.

#375 hoisted the D5 identifier list here so both build arms read one definition.
#377 closes the hole that survived that hoist: every list was snake_case
(``inchikey``), while a SHACL gap's local property name is schema.org camelCase
(``inChIKey``), so the D5 guard silently missed and the user's prose was
committed onto the exported node.
"""

from __future__ import annotations

from builder.tools.field_kinds import (
    is_identifier_field,
    normalise_field_name,
)


class TestNormaliseFieldName:
    """The key is separator-free and lowercased, NOT snake_case.

    Snake_case cannot reconcile these vocabularies: ``inChIKey`` would become
    ``in_ch_ikey``, never the ``inchikey`` the field set is written in, because
    the acronym boundaries are not recoverable. Dropping separators on both
    sides is what makes one vocabulary serve all three spellings.
    """

    def test_camelcase_and_snake_case_agree(self):
        assert normalise_field_name("inChIKey") == normalise_field_name("inchikey")
        assert normalise_field_name("molecularFormula") == normalise_field_name(
            "molecular_formula"
        )
        assert normalise_field_name("pubChemCid") == normalise_field_name("pubchem_cid")

    def test_handles_a_property_iri(self):
        assert normalise_field_name("http://schema.org/inChIKey") == "inchikey"
        assert normalise_field_name("https://schema.org/molecularFormula") == (
            "molecularformula"
        )

    def test_distinct_fields_stay_distinct(self):
        """Honesty control: normalisation must not collapse unrelated fields."""
        keys = {
            normalise_field_name(f)
            for f in ("identifier", "accession", "smiles", "name", "description")
        }
        assert len(keys) == 5

    def test_empty_and_none_are_safe(self):
        assert normalise_field_name("") == ""
        assert normalise_field_name(None) == ""


class TestIsIdentifierFieldNormalises:
    def test_camelcase_identifier_fields_are_guarded(self):
        """The D5 hole: these are exactly the two that slipped through."""
        assert is_identifier_field("inChIKey") is True
        assert is_identifier_field("molecularFormula") is True

    def test_property_iri_is_guarded(self):
        assert is_identifier_field("http://schema.org/inChIKey") is True

    def test_snake_case_identifiers_still_guarded(self):
        for field in ("identifier", "accession", "smiles", "orcid", "doi", "cas"):
            assert is_identifier_field(field) is True, field

    def test_descriptive_fields_are_not_identifiers(self):
        """Honesty control: the predicate is not a constant True."""
        for field in ("name", "description", "keywords", "measurementMethod"):
            assert is_identifier_field(field) is False, field


