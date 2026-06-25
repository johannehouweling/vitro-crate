"""Tests for builder/tools/lookups.py.

Tests the unified lookup interface that wraps the existing API clients.
Uses monkeypatching to avoid actual HTTP calls.
"""

from __future__ import annotations

import pytest

from builder.tools.lookups import (
    lookup_aop,
    lookup_bao_term,
    lookup_cell_line,
    lookup_compound,
    lookup_doi,
    lookup_dtxsid,
    lookup_ontology_term,
    lookup_orcid,
    lookup_ror,
    lookup_unit,
)


class TestLookupCompound:
    """Tests for lookup_compound — wraps PubChem with multi-strategy."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_compound.cache_clear()
        yield

    def test_returns_correct_structure_on_success(self, monkeypatch):
        def mock_lookup_pubchem(name):
            return {
                "cas": "33889-69-9",
                "smiles": "C1=CC(=C(C=C1[C@@H]2[C@H](COC3=C2C(=CC(=C3)O)O)O)O)O",
                "inchikey": "ABCDEF123456",
                "inchi": "InChI=1S/Test",
                "formula": "C25H22O10",
                "mass": "482.44",
                "iupac_name": "Silychristin A",
                "pubchem_cid": "441764",
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_pubchem", mock_lookup_pubchem)
        result = lookup_compound("Silychristin A")
        assert isinstance(result, dict)
        assert "found" in result
        assert "data" in result
        assert "error" in result
        assert result["found"] is True
        assert isinstance(result["data"], dict)
        assert result["data"]["cas"] == "33889-69-9"
        assert result["data"]["smiles"] != ""
        assert result["data"]["pubchem_cid"] == "441764"
        assert result["error"] is None

    def test_returns_correct_structure_on_failure(self, monkeypatch):
        def mock_lookup_pubchem(name):
            return {}

        chebi_calls: list[tuple[str, str]] = []

        def mock_chebi(raw, ontology):
            chebi_calls.append((raw, ontology))
            return {}

        monkeypatch.setattr("builder.tools.lookups.lookup_pubchem", mock_lookup_pubchem)
        # Keep the ChEBI fallback offline & deterministic: without this stub the
        # PubChem-miss path calls the live OLS endpoint, making this unit test
        # hit the network (CI flake — the regression #117 already fixed once).
        monkeypatch.setattr(
            "builder.tools.lookups.lookup_ontology_term_ols", mock_chebi
        )
        result = lookup_compound("NonexistentCompoundXYZ")
        assert isinstance(result, dict)
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)
        err_lower = result["error"].lower()
        assert "not found" in err_lower or "failed" in err_lower
        # The fallback was consulted (and intercepted) — proves no live call.
        assert chebi_calls == [("NonexistentCompoundXYZ", "chebi")]

    def test_falls_back_to_chebi_when_pubchem_misses(self, monkeypatch):
        # When PubChem indexes nothing, lookup_compound resolves a ChEBI IRI via
        # OLS (the AGENTS.md §10 name → CAS → ChEBI multi-strategy). Fully mocked
        # — covers the #146 fallback path with no network.
        monkeypatch.setattr("builder.tools.lookups.lookup_pubchem", lambda name: {})
        chebi_hit = {
            "@id": "http://purl.obolibrary.org/obo/CHEBI_15377",
            "termCode": "CHEBI:15377",
            "name": "water",
        }
        monkeypatch.setattr(
            "builder.tools.lookups.lookup_ontology_term_ols",
            lambda raw, ontology: chebi_hit if ontology == "chebi" else {},
        )
        result = lookup_compound("water")
        assert result["found"] is True
        assert result["data"]["source"] == "chebi"
        # ChEBI identity rides on context-declared keys (Issue #243): the IRI as a
        # ``sameAs`` @id node and the CURIE as ``chebiId`` (schema:identifier) — not
        # the bare ``chebi_iri`` / ``chebi_id`` keys that fail @context compaction.
        assert result["data"]["sameAs"] == {
            "@id": "http://purl.obolibrary.org/obo/CHEBI_15377"
        }
        assert result["data"]["chebiId"] == "CHEBI:15377"
        assert "chebi_iri" not in result["data"]
        assert "chebi_id" not in result["data"]
        assert result["data"]["iupac_name"] == "water"
        assert result["error"] is None

    def test_never_throws_exception(self, monkeypatch):
        def mock_lookup_pubchem(name):
            raise RuntimeError("API timeout")

        monkeypatch.setattr("builder.tools.lookups.lookup_pubchem", mock_lookup_pubchem)
        result = lookup_compound("ExplosiveChemical")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)

    def test_compound_multi_strategy(self, monkeypatch):
        calls = []

        def mock_lookup_pubchem(name_or_cas):
            calls.append(name_or_cas)
            if name_or_cas == "Silychristin A":
                return {
                    "cas": "33889-69-9",
                    "smiles": "C1CC=...",
                    "inchikey": "ABCDEF123456",
                    "inchi": "InChI=1S/Test",
                    "formula": "C25H22O10",
                    "mass": "482.44",
                    "iupac_name": "Silychristin A",
                    "pubchem_cid": "441764",
                }
            return {}

        monkeypatch.setattr("builder.tools.lookups.lookup_pubchem", mock_lookup_pubchem)
        result = lookup_compound("Silychristin A")
        assert result["found"] is True
        assert len(calls) == 1
        assert calls[0] == "Silychristin A"

    def test_cache_hits_return_same_result(self, monkeypatch):
        call_count = 0

        def mock_lookup_pubchem(name):
            nonlocal call_count
            call_count += 1
            return {
                "pubchem_cid": "1234",
                "cas": "50-00-0",
                "smiles": "C=O",
                "inchikey": "INCHIKEY123",
                "inchi": "InChI=1S/Test",
                "formula": "CH2O",
                "mass": "30.03",
                "iupac_name": "Formaldehyde",
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_pubchem", mock_lookup_pubchem)
        r1 = lookup_compound("Formaldehyde")
        r2 = lookup_compound("Formaldehyde")
        r3 = lookup_compound("Formaldehyde")
        assert r1["found"] is True
        assert r2["found"] is True
        assert r3["found"] is True
        assert call_count == 1


class TestLookupCellLine:
    """Tests for lookup_cell_line — wraps Cellosaurus."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_cell_line.cache_clear()
        yield

    def test_returns_correct_structure_on_success(self, monkeypatch):
        def mock_lookup_cellosaurus(accession):
            return {
                "name": "Hep G2",
                "identifier": "https://www.cellosaurus.org/CVCL_0027",
                "url": "https://www.cellosaurus.org/CVCL_0027",
                "taxonomicRange": {
                    "@id": "http://purl.obolibrary.org/obo/NCBITaxon_9606",
                    "@type": "DefinedTerm",
                    "name": "Homo sapiens",
                },
                "disease": [
                    {
                        "@id": "http://purl.obolibrary.org/obo/NCIT_C21689",
                        "@type": "DefinedTerm",
                        "name": "hepatocellular carcinoma",
                    }
                ],
                "category": "Cancer cell line",
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_cellosaurus", mock_lookup_cellosaurus)
        result = lookup_cell_line("CVCL_0027")
        assert result["found"] is True
        assert result["data"]["name"] == "Hep G2"
        assert result["error"] is None

    def test_returns_correct_structure_on_failure(self, monkeypatch):
        def mock_lookup_cellosaurus(accession):
            return {}

        monkeypatch.setattr("builder.tools.lookups.lookup_cellosaurus", mock_lookup_cellosaurus)
        result = lookup_cell_line("CVCL_9999")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)

    def test_never_throws_exception(self, monkeypatch):
        def mock_lookup_cellosaurus(accession):
            raise RuntimeError("API error")

        monkeypatch.setattr("builder.tools.lookups.lookup_cellosaurus", mock_lookup_cellosaurus)
        result = lookup_cell_line("CVCL_0000")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)


class TestLookupAOP:
    """Tests for lookup_aop — wraps AOP-Wiki."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_aop.cache_clear()
        yield

    def test_returns_correct_structure_on_success(self, monkeypatch):
        def mock_lookup_aop(aop_id):
            return {
                "aop": {
                    "@id": "https://aopwiki.org/aops/610",
                    "@type": "AdverseOutcomePathway",
                    "name": "Test AOP 610",
                    "identifier": "610",
                },
                "events": [
                    {
                        "@id": "https://aopwiki.org/events/1",
                        "@type": "KeyEvent",
                        "name": "Molecular event",
                        "eventType": "Molecular Initiating Event",
                    }
                ],
                "relationships": [],
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_aop_wiki", mock_lookup_aop)
        result = lookup_aop("610")
        assert result["found"] is True
        assert result["data"]["aop"]["name"] == "Test AOP 610"
        assert len(result["data"]["events"]) == 1
        assert result["error"] is None

    def test_returns_correct_structure_on_failure(self, monkeypatch):
        def mock_lookup_aop(aop_id):
            return {}

        monkeypatch.setattr("builder.tools.lookups.lookup_aop_wiki", mock_lookup_aop)
        result = lookup_aop("99999")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)

    def test_never_throws_exception(self, monkeypatch):
        def mock_lookup_aop(aop_id):
            raise RuntimeError("AOP-Wiki unavailable")

        monkeypatch.setattr("builder.tools.lookups.lookup_aop_wiki", mock_lookup_aop)
        result = lookup_aop("0")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)

    def test_accepts_numeric_string_or_int(self, monkeypatch):
        def mock_lookup_aop(aop_id):
            return (
                {
                    "aop": {"name": "AOP 42", "identifier": str(aop_id)},
                    "events": [],
                    "relationships": [],
                }
                if str(aop_id) == "42"
                else {}
            )

        monkeypatch.setattr("builder.tools.lookups.lookup_aop_wiki", mock_lookup_aop)
        result = lookup_aop("42")
        assert result["found"] is True
        assert result["data"]["aop"]["identifier"] == "42"


class TestLookupBAOTerm:
    """Tests for lookup_bao_term — wraps BAO/OLS."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_bao_term.cache_clear()
        yield

    def test_returns_correct_structure_on_success(self, monkeypatch):
        def mock_lookup_bao(query):
            return {
                "@id": "http://purl.obolibrary.org/obo/BAO_0000172",
                "@type": "DefinedTerm",
                "name": "gene expression assay",
                "termCode": "BAO_0000172",
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_bao_term_ols", mock_lookup_bao)
        result = lookup_bao_term("gene expression assay")
        assert result["found"] is True
        assert result["data"]["name"] == "gene expression assay"
        assert result["data"]["termCode"] == "BAO_0000172"
        assert result["error"] is None

    def test_returns_correct_structure_on_failure(self, monkeypatch):
        def mock_lookup_bao(query):
            return {}

        monkeypatch.setattr("builder.tools.lookups.lookup_bao_term_ols", mock_lookup_bao)
        result = lookup_bao_term("nonexistent")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)

    def test_never_throws_exception(self, monkeypatch):
        def mock_lookup_bao(query):
            raise RuntimeError("OLS unavailable")

        monkeypatch.setattr("builder.tools.lookups.lookup_bao_term_ols", mock_lookup_bao)
        result = lookup_bao_term("crash")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)


class TestLookupOrcid:
    """Tests for lookup_orcid — wraps ORCID."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_orcid.cache_clear()
        yield

    def test_returns_correct_structure_on_success(self, monkeypatch):
        def mock_lookup_orcid(orcid_id):
            return {
                "@id": "https://orcid.org/0000-0001-6004-8653",
                "@type": "Person",
                "identifier": "https://orcid.org/0000-0001-6004-8653",
                "givenName": "John",
                "familyName": "Doe",
                "name": "John Doe",
                "affiliation_name": "University of Example",
                "affiliation_ror": "https://ror.org/012345678",
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_orcid_api", mock_lookup_orcid)
        result = lookup_orcid("0000-0001-6004-8653")
        assert result["found"] is True
        assert result["data"]["name"] == "John Doe"
        assert result["error"] is None

    def test_returns_fallback_on_failure(self, monkeypatch):
        def mock_lookup_orcid(orcid_id):
            return {
                "@id": f"https://orcid.org/{orcid_id}",
                "@type": "Person",
                "identifier": f"https://orcid.org/{orcid_id}",
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_orcid_api", mock_lookup_orcid)
        result = lookup_orcid("0000-0000-0000-0000")
        assert result["found"] is True
        assert result["data"]["@id"] == "https://orcid.org/0000-0000-0000-0000"
        assert result["error"] is None

    def test_never_throws_exception(self, monkeypatch):
        def mock_lookup_orcid(orcid_id):
            raise RuntimeError("ORCID unavailable")

        monkeypatch.setattr("builder.tools.lookups.lookup_orcid_api", mock_lookup_orcid)
        result = lookup_orcid("0000-1111-2222-3333")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)


class TestLookupROR:
    """Tests for lookup_ror — wraps ROR."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_ror.cache_clear()
        yield

    def test_returns_correct_structure_on_success(self, monkeypatch):
        def mock_search_ror(name):
            return {
                "@id": "https://ror.org/02jz4aj89",
                "@type": "Organization",
                "name": "Maastricht University",
                "url": "https://www.maastrichtuniversity.nl",
                "identifier": "https://ror.org/02jz4aj89",
            }

        monkeypatch.setattr("builder.tools.lookups.search_ror", mock_search_ror)
        result = lookup_ror("Maastricht University")
        assert result["found"] is True
        assert result["data"]["name"] == "Maastricht University"
        assert result["error"] is None

    def test_returns_correct_structure_on_failure(self, monkeypatch):
        def mock_search_ror(name):
            return {}

        monkeypatch.setattr("builder.tools.lookups.search_ror", mock_search_ror)
        result = lookup_ror("NonexistentOrganizationXYZ")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)

    def test_never_throws_exception(self, monkeypatch):
        def mock_search_ror(name):
            raise RuntimeError("ROR unavailable")

        monkeypatch.setattr("builder.tools.lookups.search_ror", mock_search_ror)
        result = lookup_ror("crash")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)


class TestLookupDOI:
    """Tests for lookup_doi — wraps Crossref."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_doi.cache_clear()
        yield

    def test_returns_correct_structure_on_success(self, monkeypatch):
        def mock_lookup_doi(doi):
            return {
                "@id": "https://doi.org/10.1016/j.tox.2021.152898",
                "@type": "ScholarlyArticle",
                "identifier": "https://doi.org/10.1016/j.tox.2021.152898",
                "name": "A test toxicology article",
                "headline": "A test toxicology article",
                "author": [{"givenName": "Jane", "familyName": "Smith"}],
                "datePublished": "2021",
                "url": "https://doi.org/10.1016/j.tox.2021.152898",
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_doi_crossref", mock_lookup_doi)
        result = lookup_doi("10.1016/j.tox.2021.152898")
        assert result["found"] is True
        assert result["data"]["name"] == "A test toxicology article"
        assert len(result["data"]["author"]) == 1
        assert result["data"]["author"][0]["familyName"] == "Smith"
        assert result["error"] is None

    def test_returns_correct_structure_on_failure(self, monkeypatch):
        def mock_lookup_doi(doi):
            return {}

        monkeypatch.setattr("builder.tools.lookups.lookup_doi_crossref", mock_lookup_doi)
        result = lookup_doi("10.0000/nonexistent")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)

    def test_never_throws_exception(self, monkeypatch):
        def mock_lookup_doi(doi):
            raise RuntimeError("Crossref unavailable")

        monkeypatch.setattr("builder.tools.lookups.lookup_doi_crossref", mock_lookup_doi)
        result = lookup_doi("10.9999/crash")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)


class TestLookupOntologyTerm:
    """Tests for lookup_ontology_term — generic OLS ontology lookup (#142)."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_ontology_term.cache_clear()
        yield

    def test_returns_iri_and_score_on_success(self, monkeypatch):
        def mock_ols(query, ontology):
            assert ontology == "efo"
            return {
                "@id": "http://www.ebi.ac.uk/efo/EFO_0000311",
                "@type": "DefinedTerm",
                "name": "cancer",
                "termCode": "EFO_0000311",
                "score": 12.34,
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_ontology_term_ols", mock_ols)
        result = lookup_ontology_term("cancer", "efo")
        assert result["found"] is True
        assert result["data"]["@id"] == "http://www.ebi.ac.uk/efo/EFO_0000311"
        assert result["data"]["score"] == 12.34
        assert result["error"] is None

    def test_returns_failure_when_no_match(self, monkeypatch):
        monkeypatch.setattr(
            "builder.tools.lookups.lookup_ontology_term_ols", lambda q, o: {}
        )
        result = lookup_ontology_term("zzz", "efo")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)

    def test_never_throws_exception(self, monkeypatch):
        def boom(query, ontology):
            raise RuntimeError("OLS down")

        monkeypatch.setattr("builder.tools.lookups.lookup_ontology_term_ols", boom)
        result = lookup_ontology_term("crash", "efo")
        assert result["found"] is False
        assert isinstance(result["error"], str)


class TestLookupUnit:
    """Tests for lookup_unit — UO unit resolution via OLS (#142)."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_unit.cache_clear()
        yield

    def test_returns_uo_iri_on_success(self, monkeypatch):
        def mock_uo(unit_string):
            return {
                "@id": "http://purl.obolibrary.org/obo/UO_0000064",
                "@type": "DefinedTerm",
                "name": "micromolar",
                "termCode": "UO_0000064",
                "score": 9.1,
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_unit_ols", mock_uo)
        result = lookup_unit("micromolar")
        assert result["found"] is True
        assert result["data"]["@id"] == "http://purl.obolibrary.org/obo/UO_0000064"
        assert result["data"]["@id"].rsplit("/", 1)[1].startswith("UO_")
        assert result["error"] is None

    def test_returns_failure_when_no_match(self, monkeypatch):
        monkeypatch.setattr("builder.tools.lookups.lookup_unit_ols", lambda u: {})
        result = lookup_unit("notaunit")
        assert result["found"] is False
        assert isinstance(result["error"], str)

    def test_never_throws_exception(self, monkeypatch):
        def boom(unit_string):
            raise RuntimeError("OLS down")

        monkeypatch.setattr("builder.tools.lookups.lookup_unit_ols", boom)
        result = lookup_unit("crash")
        assert result["found"] is False
        assert isinstance(result["error"], str)


class TestLookupDtxsid:
    """Tests for lookup_dtxsid — EPA CompTox DTXSID resolution (#146)."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_dtxsid.cache_clear()
        yield

    def test_returns_dtxsid_on_success(self, monkeypatch):
        def mock_comptox(query):
            return {
                "dtxsid": "DTXSID7020182",
                "@id": "https://comptox.epa.gov/dashboard/chemical/details/DTXSID7020182",
                "@type": "MolecularEntity",
                "name": "Bisphenol A",
                "casrn": "80-05-7",
                "inchikey": "IISBACLAFKSPIT-UHFFFAOYSA-N",
            }

        monkeypatch.setattr("builder.tools.lookups.lookup_dtxsid_comptox", mock_comptox)
        result = lookup_dtxsid("Bisphenol A")
        assert result["found"] is True
        assert result["data"]["dtxsid"] == "DTXSID7020182"
        assert result["data"]["casrn"] == "80-05-7"
        assert result["error"] is None

    def test_returns_failure_when_no_match(self, monkeypatch):
        monkeypatch.setattr("builder.tools.lookups.lookup_dtxsid_comptox", lambda q: {})
        result = lookup_dtxsid("NotAChemical")
        assert result["found"] is False
        assert result["data"] == {}
        assert isinstance(result["error"], str)

    def test_never_throws_exception(self, monkeypatch):
        def boom(query):
            raise RuntimeError("CompTox down")

        monkeypatch.setattr("builder.tools.lookups.lookup_dtxsid_comptox", boom)
        result = lookup_dtxsid("crash")
        assert result["found"] is False
        assert isinstance(result["error"], str)


class TestLookupCompoundChebiFallback:
    """lookup_compound falls back to ChEBI (via OLS) when PubChem misses (#146)."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        lookup_compound.cache_clear()
        yield

    def test_falls_back_to_chebi_when_pubchem_empty(self, monkeypatch):
        monkeypatch.setattr("builder.tools.lookups.lookup_pubchem", lambda q: {})

        def mock_chebi(query, ontology):
            assert ontology == "chebi"
            return {
                "@id": "http://purl.obolibrary.org/obo/CHEBI_28061",
                "@type": "DefinedTerm",
                "name": "alpha-D-glucose",
                "termCode": "CHEBI_28061",
            }

        monkeypatch.setattr(
            "builder.tools.lookups.lookup_ontology_term_ols", mock_chebi
        )
        result = lookup_compound("alpha-D-glucose")
        assert result["found"] is True
        # Context-valid ChEBI identity (Issue #243): CURIE under ``chebiId``,
        # ontology IRI under a ``sameAs`` @id node.
        assert result["data"]["chebiId"] == "CHEBI_28061"
        assert result["data"]["sameAs"] == {
            "@id": "http://purl.obolibrary.org/obo/CHEBI_28061"
        }
        assert result["data"]["source"] == "chebi"

    def test_fails_when_both_pubchem_and_chebi_miss(self, monkeypatch):
        monkeypatch.setattr("builder.tools.lookups.lookup_pubchem", lambda q: {})
        monkeypatch.setattr(
            "builder.tools.lookups.lookup_ontology_term_ols", lambda q, o: {}
        )
        result = lookup_compound("TotallyMadeUpXYZ")
        assert result["found"] is False
        assert isinstance(result["error"], str)
