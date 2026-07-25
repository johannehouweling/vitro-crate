"""Issue #358: ontology term IRIs come from one canonical source
(``profiles.ontology_iris``), using each ontology's native IRI — never a
``bioregistry.io`` meta-resolver URL — and no emitter file inlines raw ontology IRIs.
"""

from __future__ import annotations

import pathlib

import pytest

from profiles.ontology_iris import PREFIXES, iri

REPO = pathlib.Path(__file__).resolve().parents[1]

# Every file that emits ontology term IRIs must build them via ``iri()`` — no raw
# ontology-term literals — so there is exactly one place to edit an ontology base.
_EMITTER_FILES = [
    "profiles/models/tox.py",
    "builder/tools/_crate_mapping.py",
    "profiles/context.py",
    "builder/tools/drafters.py",
    "builder/agents/react/tools_spec.py",
]

def _grep(rel: str, needle: str) -> list[str]:
    return [
        line.strip()
        for line in (REPO / rel).read_text().splitlines()
        if needle in line and not line.lstrip().startswith("#")
    ]


class TestCurieExpansion:
    def test_obo_ontologies_expand_to_obo_purl(self):
        assert iri("NCIT:C83280") == "http://purl.obolibrary.org/obo/NCIT_C83280"
        assert iri("CHEBI:23367") == "http://purl.obolibrary.org/obo/CHEBI_23367"
        assert iri("MSIO:0000062") == "http://purl.obolibrary.org/obo/MSIO_0000062"
        assert iri("PATO:0000033") == "http://purl.obolibrary.org/obo/PATO_0000033"
        assert iri("UO:0000064") == "http://purl.obolibrary.org/obo/UO_0000064"

    def test_bao_uses_its_native_namespace(self):
        assert iri("BAO:0000697") == "http://www.bioassayontology.org/bao#BAO_0000697"

    def test_efo_uses_ebi_native_namespace(self):
        # EFO is an EBI application ontology; its native IRI is under ebi.ac.uk/efo,
        # not the OBO PURL and certainly not a bioregistry.io resolver URL.
        assert iri("EFO:0002090") == "http://www.ebi.ac.uk/efo/EFO_0002090"

    def test_no_prefix_is_a_meta_resolver_url(self):
        assert all(
            "bioregistry.io" not in ns and "identifiers.org" not in ns
            for ns in PREFIXES.values()
        )

    def test_unknown_or_malformed_curie_raises(self):
        with pytest.raises(KeyError):
            iri("NOPE:123")
        with pytest.raises(KeyError):
            iri("NCIT_C83280")  # no ':' separator


class TestEmittersAvoidAntiPatterns:
    """The two IRI anti-patterns #358 fixes must not reappear in the emitter files.

    (Ontology-IRI mentions inside comments, docstrings, or LLM-facing tool-description
    examples are fine; these guards target IRIs used as data.)
    """

    def test_no_bioregistry_resolver_urls(self):
        """A term's identity is its ontology's own IRI, never a bioregistry.io redirect."""
        offenders = {
            rel: hits
            for rel in _EMITTER_FILES
            if (hits := _grep(rel, "bioregistry.io/"))
        }
        assert not offenders, (
            "bioregistry.io resolver URLs are not canonical term identities; "
            f"build the native IRI via ontology_iris.iri(): {offenders}"
        )

    def test_bao_uses_native_namespace_not_obo_purl(self):
        """BAO is not in the OBO PURL system; use its bioassayontology.org namespace."""
        offenders = {
            rel: hits
            for rel in _EMITTER_FILES
            if (hits := _grep(rel, "purl.obolibrary.org/obo/BAO_"))
        }
        assert not offenders, (
            f"BAO must use its native bao# namespace via ontology_iris.iri(): {offenders}"
        )
