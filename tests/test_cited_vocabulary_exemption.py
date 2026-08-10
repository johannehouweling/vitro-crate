"""Findings on CITED vocabulary terms are set aside; findings on DATA are not.

The RO-Crate validator exempts a fixed list of vocabulary namespaces from
"SHOULD have a schema.org type / a name / be described in the same @graph" —
w3.org, schema.org, purl.org, bioschemas.org, urn:. That list stops at the
vocabularies a *workflow* crate is built from. A toxicology crate is built from
OBO, BAO, EFO and the AOP-Wiki ontology, which appear in exactly the same
position (a ``propertyID`` on a PropertyValue) and get flagged where Dublin Core
does not.

This module pins the local stand-in for that gap. The tests that matter are the
NEGATIVE ones: an exemption that is too wide buries real findings, and buried
findings are worse than noisy ones because nothing shows they were suppressed.
"""

from __future__ import annotations

import logging

from builder.tools.validation import _CITED_VOCABULARY_NAMESPACES, _is_cited_vocabulary


class TestCitedVocabularyIsExempt:
    """The four namespaces the tox profile is built from."""

    def test_each_declared_namespace_is_recognised(self):
        for ns in _CITED_VOCABULARY_NAMESPACES:
            assert _is_cited_vocabulary(f"{ns}SOME_TERM"), ns

    def test_representative_terms(self):
        for term in (
            "http://purl.obolibrary.org/obo/NCIT_C16403",
            "http://www.bioassayontology.org/bao#BAO_0002993",
            "http://www.ebi.ac.uk/efo/EFO_0002091",
            "https://aopwiki.org/ontology/hasKeyEvent",
        ):
            assert _is_cited_vocabulary(term), term


class TestDataIdentifiersAreNotExempt:
    """THE safety property: identifiers naming data the crate claims about stay.

    Exempting these would bury the missing names, missing emails and unreferenced
    compounds that are ours to fix. Measured on one crate: 22 of 26 PubChem
    findings were orphaned compounds, not vocabulary noise — so a wider
    exemption would have hidden 22 real defects to silence 4 spurious ones.
    """

    def test_person_and_org_identifiers_stay(self):
        for term in ("https://orcid.org/0000-0002-1825-0097", "https://ror.org/04pp8hn57"):
            assert not _is_cited_vocabulary(term), term

    def test_compound_identifiers_stay(self):
        for term in (
            "https://pubchem.ncbi.nlm.nih.gov/compound/712",
            "https://comptox.epa.gov/dashboard/chemical/details/DTXSID7020182",
        ):
            assert not _is_cited_vocabulary(term), term

    def test_in_crate_entities_stay(self):
        for term in ("#MolecularEntity_chem_1", "./", "data/raw.csv"):
            assert not _is_cited_vocabulary(term), term

    def test_a_lookalike_host_is_not_matched(self):
        """Prefix matching is on the full namespace, not a bare hostname.

        `evil-purl.obolibrary.org` and a path that merely CONTAINS the namespace
        must not slip through — the check anchors at the start.
        """
        for term in (
            "https://evil.example/http://purl.obolibrary.org/obo/X",
            "http://purl.obolibrary.org.attacker.test/obo/X",
        ):
            assert not _is_cited_vocabulary(term), term

    def test_non_string_ids_are_not_exempt(self):
        for value in (None, 42, [], {"@id": "http://purl.obolibrary.org/obo/X"}):
            assert not _is_cited_vocabulary(value), value


class TestSuppressionIsVisible:
    """Set aside is not the same as silently dropped."""

    def test_the_count_is_logged(self, caplog, monkeypatch):
        """An auditor asking "why does this crate report so little?" must find it.

        This is OUR exemption, not the validator's verdict, so it has to leave a
        trace. A filter that removes findings without saying so is how a crate
        comes to look cleaner than it is.
        """
        from builder.state import CrateState
        from builder.tools import validation as v

        class _Issue:
            entity_id = "http://purl.obolibrary.org/obo/NCIT_C16403"
            property = "name"
            message = "should have a name"
            severity = "recommended"
            profile = "base"

        class _Result:
            profile = "base"
            passed_required = True
            issues = [_Issue()]

        monkeypatch.setattr(v, "_assemble_and_validate", lambda *a, **k: ({}, [_Result()]))
        with caplog.at_level(logging.INFO, logger="builder.tools.validation"):
            out = v.build_and_validate(CrateState())

        assert out["issues"] == [], "a cited-vocabulary finding must not be routed"
        assert out["ok"] is True, "an exempt-only result conforms"
        assert any("Set aside 1 finding" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]
