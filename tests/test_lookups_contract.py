"""Contract tests for raw lookup API clients.

Uses the ``responses`` library to replay recorded HTTP fixtures against
the real parsing logic in each lookup module. Each test class covers:
  - Success path (realistic fixture)
  - Empty / missing data
  - Error / timeout handling
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses
from requests.exceptions import ConnectionError, Timeout

from lookups._http import TransientLookupError
from lookups.aopwiki import lookup_aop
from lookups.bao import lookup_bao_term
from lookups.cellosaurus import lookup_cellosaurus
from lookups.crossref import lookup_doi
from lookups.orcid import lookup_orcid
from lookups.pubchem import lookup_pubchem
from lookups.ror import search_ror

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Helpers to clear LRU caches between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear all lookup LRU caches before each test."""
    lookup_pubchem.cache_clear()
    lookup_cellosaurus.cache_clear()
    lookup_aop.cache_clear()
    lookup_bao_term.cache_clear()
    lookup_orcid.cache_clear()
    search_ror.cache_clear()
    lookup_doi.cache_clear()
    yield


# ===========================================================================
# PubChem
# ===========================================================================

class TestPubChemContract:
    """Contract tests for lookups/pubchem.py."""

    @responses.activate
    def test_success_parses_compound(self):
        """Realistic PubChem response is parsed into expected shape."""
        responses.add(
            responses.GET,
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/taxifolin/JSON",
            json=_load("pubchem_compound.json"),
            status=200,
        )
        responses.add(
            responses.GET,
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/taxifolin/synonyms/JSON",
            json=_load("pubchem_synonyms.json"),
            status=200,
        )

        result = lookup_pubchem("taxifolin")

        assert result["pubchem_cid"] == "5280863"
        assert result["formula"] == "C15H12O5"
        assert result["mass"] == "272.25"
        assert "VEEGZPWAAPPXRB" in result["inchikey"]
        assert result["smiles"] != ""
        assert result["inchi"].startswith("InChI=")
        assert result["iupac_name"] != ""
        assert result["cas"] == "480-18-2"

    @responses.activate
    def test_not_found_returns_empty(self):
        """A 404 from PubChem returns empty dict."""
        responses.add(
            responses.GET,
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/nonexistent999xyz/JSON",
            status=404,
        )

        result = lookup_pubchem("nonexistent999xyz")
        assert result == {}

    @responses.activate
    def test_timeout_returns_empty(self):
        """A timeout returns empty dict without raising."""
        responses.add(
            responses.GET,
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/taxifolin/JSON",
            body=Timeout("Connection timed out"),
        )

        with pytest.raises(TransientLookupError):
            lookup_pubchem("taxifolin")

    @responses.activate
    def test_malformed_json_returns_empty(self):
        """Malformed JSON returns empty dict."""
        responses.add(
            responses.GET,
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/taxifolin/JSON",
            body="not json",
            status=200,
            content_type="application/json",
        )

        with pytest.raises(TransientLookupError):
            lookup_pubchem("taxifolin")

    @responses.activate
    def test_missing_synonyms_still_returns_compound(self):
        """If synonyms endpoint fails, compound data is still returned (no CAS)."""
        responses.add(
            responses.GET,
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/taxifolin/JSON",
            json=_load("pubchem_compound.json"),
            status=200,
        )
        responses.add(
            responses.GET,
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/taxifolin/synonyms/JSON",
            status=500,
        )

        result = lookup_pubchem("taxifolin")
        assert result["pubchem_cid"] == "5280863"
        assert result["cas"] == ""


# ===========================================================================
# Cellosaurus
# ===========================================================================

class TestCellosaurusContract:
    """Contract tests for lookups/cellosaurus.py."""

    @responses.activate
    def test_success_parses_cell_line(self):
        """Realistic Cellosaurus response is parsed correctly."""
        responses.add(
            responses.GET,
            "https://api.cellosaurus.org/cell-line/CVCL_0027?format=json",
            json=_load("cellosaurus_hepg2.json"),
            status=200,
        )

        result = lookup_cellosaurus("CVCL_0027")

        assert result["name"] == "HepG2"
        assert "CVCL_0027" in result["url"]
        assert result["identifier"] == result["url"]
        assert "Hep G2" in result["alternateName"]
        # Species
        assert result["taxonomicRange"]["@type"] == "DefinedTerm"
        assert "NCBITaxon" in result["taxonomicRange"]["@id"]
        # Disease
        assert len(result["disease"]) == 1
        assert "NCIT" in result["disease"][0]["@id"]
        # Anatomical site
        assert result["anatomicalSite"]["@type"] == "DefinedTerm"
        assert "UBERON" in result["anatomicalSite"]["@id"]
        # Donor info
        assert result["donorSex"] == "Male"
        assert result["donorAge"] == "15Y"
        assert result["category"] == "Cancer cell line"
        # sameAs
        assert len(result["sameAs"]) == 2  # CLO + Wikidata (ATCC has null iri)

    @responses.activate
    def test_not_found_returns_empty(self):
        """A 404 returns empty dict."""
        responses.add(
            responses.GET,
            "https://api.cellosaurus.org/cell-line/CVCL_9999?format=json",
            status=404,
        )

        result = lookup_cellosaurus("CVCL_9999")
        assert result == {}

    @responses.activate
    def test_timeout_returns_empty(self):
        """A timeout returns empty dict."""
        responses.add(
            responses.GET,
            "https://api.cellosaurus.org/cell-line/CVCL_0027?format=json",
            body=Timeout("timed out"),
        )

        with pytest.raises(TransientLookupError):
            lookup_cellosaurus("CVCL_0027")

    @responses.activate
    def test_minimal_response_no_optional_fields(self):
        """A cell line with only name-list parses without error."""
        minimal = {
            "Cellosaurus": {
                "cell-line-list": [
                    {
                        "name-list": [
                            {"type": "identifier", "value": "MinimalCell"}
                        ]
                    }
                ]
            }
        }
        responses.add(
            responses.GET,
            "https://api.cellosaurus.org/cell-line/CVCL_XXXX?format=json",
            json=minimal,
            status=200,
        )

        result = lookup_cellosaurus("CVCL_XXXX")
        assert result["name"] == "MinimalCell"
        assert "disease" not in result
        assert "donorSex" not in result


# ===========================================================================
# AOP-Wiki
# ===========================================================================

class TestAOPWikiContract:
    """Contract tests for lookups/aopwiki.py."""

    @responses.activate
    def test_success_parses_full_aop(self):
        """Realistic AOP-Wiki response is parsed into aop/events/relationships."""
        from lookups.aopwiki import _event_details

        _event_details.cache_clear()

        responses.add(
            responses.GET,
            "https://aopwiki.org/aops/610.json",
            json=_load("aopwiki_aop610.json"),
            status=200,
        )
        # Mock event detail calls
        event_detail = _load("aopwiki_event888.json")
        responses.add(
            responses.GET,
            "https://aopwiki.org/events/888.json",
            json=event_detail,
            status=200,
        )
        # Other events return 404 for minimal test
        responses.add(responses.GET, "https://aopwiki.org/events/177.json", status=404)
        responses.add(responses.GET, "https://aopwiki.org/events/889.json", status=404)
        responses.add(responses.GET, "https://aopwiki.org/events/890.json", status=404)

        result = lookup_aop("610")

        assert result["aop"]["@type"] == "AdverseOutcomePathway"
        assert result["aop"]["identifier"] == "610"
        assert "Inhibition" in result["aop"]["name"]
        assert result["aop"]["alternateName"] == "MC-I inhibition to Parkinsonism"
        # Events
        assert len(result["events"]) == 4  # 1 MIE + 2 KE + 1 AO
        mie = next(e for e in result["events"] if e["identifier"] == "888")
        assert mie["eventType"] == "Molecular Initiating Event"
        assert mie["short_name"] == (
            "Binding of inhibitor, NADH-ubiquinone oxidoreductase"
        )
        assert mie["biologicalOrganization"] == "Molecular"
        # Relationships
        assert len(result["relationships"]) == 3
        rel = result["relationships"][0]
        assert rel["@type"] == "KeyEventRelationship"
        assert "upstream_event" in rel
        assert "downstream_event" in rel

    @responses.activate
    def test_not_found_returns_empty(self):
        """A 404 returns empty dict."""
        responses.add(
            responses.GET,
            "https://aopwiki.org/aops/99999.json",
            status=404,
        )

        result = lookup_aop("99999")
        assert result == {}

    @responses.activate
    def test_timeout_returns_empty(self):
        """A timeout returns empty dict."""
        responses.add(
            responses.GET,
            "https://aopwiki.org/aops/610.json",
            body=ConnectionError("Connection refused"),
        )

        with pytest.raises(TransientLookupError):
            lookup_aop("610")

    @responses.activate
    def test_empty_events_still_returns_aop(self):
        """An AOP with no events still returns valid structure."""
        from lookups.aopwiki import _event_details

        _event_details.cache_clear()

        minimal = {
            "aop": {
                "title": "Empty AOP",
                "aop_mies": [],
                "aop_kes": [],
                "aop_aos": [],
                "relationships": [],
            }
        }
        responses.add(
            responses.GET,
            "https://aopwiki.org/aops/1.json",
            json=minimal,
            status=200,
        )

        result = lookup_aop("1")
        assert result["aop"]["name"] == "Empty AOP"
        assert result["events"] == []
        assert result["relationships"] == []


# ===========================================================================
# BAO / OLS
# ===========================================================================

class TestBAOContract:
    """Contract tests for lookups/bao.py."""

    @responses.activate
    def test_success_parses_term(self):
        """Realistic OLS response parsed into DefinedTerm shape."""
        responses.add(
            responses.GET,
            "https://www.ebi.ac.uk/ols4/api/search",
            json=_load("bao_cell_viability.json"),
            status=200,
        )

        result = lookup_bao_term("cell viability assay")

        assert result["@id"] == "http://www.bioassayontology.org/bao#BAO_0003009"
        assert result["@type"] == "DefinedTerm"
        assert result["name"] == "cell viability assay"
        assert result["termCode"] == "BAO_0003009"

    @responses.activate
    def test_no_results_returns_empty(self):
        """Empty results list returns empty dict."""
        responses.add(
            responses.GET,
            "https://www.ebi.ac.uk/ols4/api/search",
            json={"response": {"numFound": 0, "docs": []}},
            status=200,
        )

        result = lookup_bao_term("xyznonexistent")
        assert result == {}

    @responses.activate
    def test_empty_query_returns_empty(self):
        """Empty string query returns empty dict without HTTP call."""
        result = lookup_bao_term("")
        assert result == {}

    @responses.activate
    def test_timeout_returns_empty(self):
        """A timeout returns empty dict."""
        responses.add(
            responses.GET,
            "https://www.ebi.ac.uk/ols4/api/search",
            body=Timeout("timed out"),
        )

        with pytest.raises(TransientLookupError):
            lookup_bao_term("cell viability")

    @responses.activate
    def test_missing_iri_returns_empty(self):
        """A doc without an IRI field is skipped."""
        responses.add(
            responses.GET,
            "https://www.ebi.ac.uk/ols4/api/search",
            json={"response": {"numFound": 1, "docs": [
                {"label": "something", "iri": ""},
            ]}},
            status=200,
        )

        result = lookup_bao_term("something")
        assert result == {}


# ===========================================================================
# ORCID
# ===========================================================================

class TestORCIDContract:
    """Contract tests for lookups/orcid.py."""

    @responses.activate
    def test_success_parses_record(self):
        """Realistic ORCID response parsed into Person shape."""
        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/0000-0001-6004-8653/record",
            json=_load("orcid_record.json"),
            status=200,
        )

        result = lookup_orcid("0000-0001-6004-8653")

        assert result["@type"] == "Person"
        assert result["@id"] == "https://orcid.org/0000-0001-6004-8653"
        assert result["givenName"] == "Jan"
        assert result["familyName"] == "de Vries"
        assert result["name"] == "Jan de Vries"
        assert result["affiliation_name"] == "Maastricht University"
        assert result["affiliation_ror"] == "https://ror.org/02jz4aj89"

    @responses.activate
    def test_not_found_returns_fallback(self):
        """A 404 returns fallback with just @id and identifier."""
        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/0000-0000-0000-0000/record",
            status=404,
        )

        result = lookup_orcid("0000-0000-0000-0000")
        assert result == {}

    @responses.activate
    def test_timeout_returns_fallback(self):
        """A timeout returns fallback."""
        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/0000-0001-6004-8653/record",
            body=Timeout("timed out"),
        )

        with pytest.raises(TransientLookupError):
            lookup_orcid("0000-0001-6004-8653")

    @responses.activate
    def test_no_affiliation(self):
        """Record without employment returns empty affiliation fields."""
        record = {
            "person": {
                "name": {
                    "given-names": {"value": "Test"},
                    "family-name": {"value": "User"},
                }
            },
            "activities-summary": {"employments": {"affiliation-group": []}},
        }
        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/0000-0002-0000-0000/record",
            json=record,
            status=200,
        )

        result = lookup_orcid("0000-0002-0000-0000")
        assert result["name"] == "Test User"
        assert result["affiliation_name"] == ""
        assert result["affiliation_ror"] == ""


# ===========================================================================
# ROR
# ===========================================================================

class TestRORContract:
    """Contract tests for lookups/ror.py."""

    @responses.activate
    def test_success_parses_organization(self):
        """Realistic ROR response parsed into Organization shape."""
        responses.add(
            responses.GET,
            "https://api.ror.org/organizations",
            json=_load("ror_maastricht.json"),
            status=200,
        )

        result = search_ror("Maastricht University")

        assert result["@id"] == "https://ror.org/02jz4aj89"
        assert result["@type"] == "Organization"
        assert result["name"] == "Maastricht University"
        assert result["url"] == "https://www.maastrichtuniversity.nl"
        assert result["identifier"] == "https://ror.org/02jz4aj89"

    @responses.activate
    def test_no_results_returns_empty(self):
        """Empty results list returns empty dict."""
        responses.add(
            responses.GET,
            "https://api.ror.org/organizations",
            json={"items": []},
            status=200,
        )

        result = search_ror("NonexistentOrg12345")
        assert result == {}

    @responses.activate
    def test_empty_name_returns_empty(self):
        """Empty name returns empty dict without HTTP call."""
        result = search_ror("")
        assert result == {}

    @responses.activate
    def test_timeout_returns_empty(self):
        """A timeout returns empty dict."""
        responses.add(
            responses.GET,
            "https://api.ror.org/organizations",
            body=Timeout("timed out"),
        )

        with pytest.raises(TransientLookupError):
            search_ror("Maastricht University")

    @responses.activate
    def test_server_error_returns_empty(self):
        """A 500 returns empty dict."""
        responses.add(
            responses.GET,
            "https://api.ror.org/organizations",
            status=500,
        )

        with pytest.raises(TransientLookupError):
            search_ror("Maastricht University")


# ===========================================================================
# Crossref
# ===========================================================================

class TestCrossrefContract:
    """Contract tests for lookups/crossref.py."""

    @responses.activate
    def test_success_parses_work(self):
        """Realistic Crossref response parsed into ScholarlyArticle shape."""
        responses.add(
            responses.GET,
            "https://api.crossref.org/works/10.1016/j.tox.2021.152898",
            json=_load("crossref_doi.json"),
            status=200,
        )

        result = lookup_doi("10.1016/j.tox.2021.152898")

        assert result["@id"] == "https://doi.org/10.1016/j.tox.2021.152898"
        assert result["@type"] == "ScholarlyArticle"
        assert "nephrotoxicity" in result["name"]
        assert result["headline"] == result["name"]
        assert result["datePublished"] == "2021"
        assert len(result["author"]) == 2
        assert result["author"][0]["familyName"] == "Krebs"
        assert result["author"][0]["identifier"] == "https://orcid.org/0000-0001-1234-5678"
        assert result["author"][1]["familyName"] == "Smith"
        assert "identifier" not in result["author"][1]

    @responses.activate
    def test_doi_prefix_stripped(self):
        """DOI with https://doi.org/ prefix is handled."""
        responses.add(
            responses.GET,
            "https://api.crossref.org/works/10.1016/j.tox.2021.152898",
            json=_load("crossref_doi.json"),
            status=200,
        )

        result = lookup_doi("https://doi.org/10.1016/j.tox.2021.152898")
        assert result["@type"] == "ScholarlyArticle"

    @responses.activate
    def test_not_found_returns_empty(self):
        """A 404 returns empty dict."""
        responses.add(
            responses.GET,
            "https://api.crossref.org/works/10.9999/nonexistent",
            status=404,
        )

        result = lookup_doi("10.9999/nonexistent")
        assert result == {}

    @responses.activate
    def test_timeout_returns_empty(self):
        """A timeout returns empty dict."""
        responses.add(
            responses.GET,
            "https://api.crossref.org/works/10.1016/j.tox.2021.152898",
            body=Timeout("timed out"),
        )

        with pytest.raises(TransientLookupError):
            lookup_doi("10.1016/j.tox.2021.152898")

    @responses.activate
    def test_empty_title_and_authors(self):
        """Work with missing title/authors still returns basic shape."""
        minimal_msg = {
            "status": "ok",
            "message": {
                "DOI": "10.1000/test",
                "title": [],
                "author": [],
                "issued": {"date-parts": [[]]},
                "URL": "https://doi.org/10.1000/test",
            },
        }
        responses.add(
            responses.GET,
            "https://api.crossref.org/works/10.1000/test",
            json=minimal_msg,
            status=200,
        )

        result = lookup_doi("10.1000/test")
        assert result["@id"] == "https://doi.org/10.1000/test"
        assert result["name"] == ""
        assert result["author"] == []
        assert result["datePublished"] == ""
