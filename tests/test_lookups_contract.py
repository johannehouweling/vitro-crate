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
from urllib.parse import parse_qs, urlsplit

import pytest
import responses
from requests.exceptions import ConnectionError, Timeout
from responses import matchers

from lookups._http import TransientLookupError
from lookups.aopwiki import lookup_aop
from lookups.bao import lookup_bao_term, lookup_ontology_term, lookup_unit
from lookups.cellosaurus import lookup_cellosaurus, search_cellosaurus
from lookups.comptox import lookup_dtxsid
from lookups.crossref import lookup_doi, search_works_by_title
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
    search_cellosaurus.cache_clear()
    lookup_aop.cache_clear()
    lookup_ontology_term.cache_clear()
    lookup_dtxsid.cache_clear()
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
        # Pinned to the exact value, not `!= ""`: PubChem renamed this property
        # and the parser silently stopped matching it, so every crate lost its
        # SMILES while this test kept passing against a stale fixture (#425).
        # The stereochemical ("Absolute") form is the one that agrees with the
        # stereochemical InChI stored alongside it.
        assert result["smiles"] == "C1=CC(=C(C=C1[C@@H]2[C@H](C(=O)C3=C(O2)C=C(C=C3)O)O)O)O"
        assert result["inchi"].startswith("InChI=")
        assert result["iupac_name"] != ""
        assert result["cas"] == "480-18-2"

    @staticmethod
    def _compound_with_smiles(*variants: tuple[str, str]) -> dict:
        """A minimal PC_Compounds payload carrying only the given SMILES props.

        ``variants`` are ``(urn.name, value)`` pairs, in the order PubChem would
        emit them.
        """
        return {
            "PC_Compounds": [
                {
                    "id": {"id": {"cid": 1}},
                    "props": [
                        {"urn": {"label": "SMILES", "name": key}, "value": {"sval": value}}
                        for key, value in variants
                    ],
                }
            ]
        }

    def _smiles_for(self, *variants: tuple[str, str], name: str = "x") -> str:
        """Parse a payload carrying only *variants* and return the SMILES chosen.

        ``name`` varies per call because ``lookup_pubchem`` is ``lru_cache``d on
        it — two calls under one name in a single test would replay the first
        result rather than parse the second payload.
        """
        responses.add(
            responses.GET,
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{name}/JSON",
            json=self._compound_with_smiles(*variants),
            status=200,
        )
        responses.add(
            responses.GET,
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{name}/synonyms/JSON",
            status=404,
        )
        return lookup_pubchem(name)["smiles"]

    @responses.activate
    def test_prefers_the_stereochemical_smiles(self):
        """`Absolute` wins over `Connectivity` however they are ordered.

        We store a stereochemical InChI; a flat SMILES beside it would describe
        a different molecule.
        """
        assert self._smiles_for(("Connectivity", "FLAT"), ("Absolute", "STEREO")) == "STEREO"

    @responses.activate
    def test_falls_back_to_connectivity_when_there_is_no_stereo_form(self):
        """An achiral compound has no `Absolute` form — it must not come back
        empty just because the preferred variant is absent."""
        assert self._smiles_for(("Connectivity", "FLAT")) == "FLAT"

    @responses.activate
    def test_still_accepts_the_retired_canonical_name(self):
        """A cached or mirrored older response must keep parsing."""
        assert self._smiles_for(("Canonical", "LEGACY")) == "LEGACY"

    @responses.activate
    def test_an_unknown_smiles_variant_is_not_taken(self):
        """Only known variants are accepted, so a future PubChem addition cannot
        silently displace the one we deliberately prefer."""
        assert (
            self._smiles_for(("Absolute", "STEREO"), ("SomeNewVariant", "OTHER"), name="a")
            == "STEREO"
        )
        assert self._smiles_for(("SomeNewVariant", "OTHER"), name="b") == ""

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

        # Cellosaurus's primary identifier for CVCL_0027 is "Hep-G2"; the far
        # more common "HepG2" is only a synonym (#385).
        assert result["name"] == "Hep-G2"
        assert "CVCL_0027" in result["url"]
        assert result["identifier"] == result["url"]
        assert "HepG2" in result["alternateName"]
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
                "cell-line-list": [{"name-list": [{"type": "identifier", "value": "MinimalCell"}]}]
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
# Cellosaurus name search (search_cellosaurus)
# ===========================================================================


class TestCellosaurusSearchContract:
    """Contract tests for lookups.cellosaurus.search_cellosaurus (name → accession).

    The client issues one request per Solr name field and unions the results
    (#385), so a test that wants different bodies per field has to route on the
    ``q`` parameter; the failure-path tests below deliberately register one
    catch-all response that serves both requests.
    """

    _SEARCH_URL = "https://api.cellosaurus.org/search/cell-line"

    @classmethod
    def _route(cls, query: str, fixture: str) -> None:
        """Serve ``fixture`` for exactly one Solr ``q`` value."""
        responses.add(
            responses.GET,
            cls._SEARCH_URL,
            match=[matchers.query_param_matcher({"q": query}, strict_match=False)],
            json=_load(fixture),
            status=200,
        )

    @responses.activate
    def test_search_returns_ranked_candidates(self):
        """A name search parses each match into {accession, name, synonyms}."""
        self._route('id:"HepG2"', "cellosaurus_search_id_hepg2.json")
        self._route('sy:"HepG2"', "cellosaurus_search_sy_hepg2.json")

        candidates = search_cellosaurus("HepG2")

        # A tuple (immutable cached value), one entry per matching cell line.
        assert isinstance(candidates, tuple)
        # 50 identifier hits + 45 synonym hits, minus 17 entries both fields
        # return — a duplicate would read to the caller's gate as a second
        # exact match and turn a resolvable name ambiguous.
        assert len(candidates) == 78
        # ``id`` is queried first, so the union opens on its top-ranked hit.
        assert candidates[0]["accession"] == "CVCL_W371"
        assert candidates[0]["name"] == "HepG2 hALR"
        # The parent reaches the list only through the synonym field, and its
        # primary identifier is "Hep-G2" — "HepG2" is a synonym.
        parent = next(c for c in candidates if c["accession"] == "CVCL_0027")
        assert parent["name"] == "Hep-G2"
        assert "HepG2" in parent["synonyms"]

    @responses.activate
    def test_search_sends_one_query_per_name_field(self):
        """Each request carries a single-field Solr query and asks for JSON."""
        self._route('id:"HepG2"', "cellosaurus_search_id_hepg2.json")
        self._route('sy:"HepG2"', "cellosaurus_search_sy_hepg2.json")

        search_cellosaurus("HepG2")

        sent = [call.request.url or "" for call in responses.calls]
        assert len(sent) == 2
        assert {parse_qs(urlsplit(url).query)["q"][0] for url in sent} == {
            'id:"HepG2"',
            'sy:"HepG2"',
        }
        assert all("format=json" in url for url in sent)

    @responses.activate
    def test_search_blank_name_no_http_returns_empty(self):
        """A blank name short-circuits with no HTTP call."""
        assert search_cellosaurus("   ") == ()
        assert len(responses.calls) == 0

    @responses.activate
    def test_search_no_matches_returns_empty(self):
        """An empty cell-line-list on both fields yields an empty tuple."""
        responses.add(
            responses.GET,
            self._SEARCH_URL,
            json={"Cellosaurus": {"cell-line-list": []}},
            status=200,
        )

        assert search_cellosaurus("NoSuchCellLineXYZ") == ()
        assert len(responses.calls) == 2

    @responses.activate
    def test_search_not_found_returns_empty(self):
        """A 404 on both fields returns an empty tuple (definitive not-found)."""
        responses.add(
            responses.GET,
            self._SEARCH_URL,
            status=404,
        )

        assert search_cellosaurus("HepG2") == ()
        assert len(responses.calls) == 2

    @responses.activate
    def test_search_timeout_raises_transient(self):
        """A timeout raises TransientLookupError, never a silent empty."""
        responses.add(
            responses.GET,
            self._SEARCH_URL,
            body=Timeout("timed out"),
        )

        with pytest.raises(TransientLookupError):
            search_cellosaurus("HepG2")


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
        assert mie["short_name"] == ("Binding of inhibitor, NADH-ubiquinone oxidoreductase")
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
            json={
                "response": {
                    "numFound": 1,
                    "docs": [
                        {"label": "something", "iri": ""},
                    ],
                }
            },
            status=200,
        )

        result = lookup_bao_term("something")
        assert result == {}


# ===========================================================================
# Generic OLS ontology term + units (#142)
# ===========================================================================


class TestOntologyTermContract:
    """Contract tests for lookups/bao.py generic OLS functions."""

    @responses.activate
    def test_generic_ontology_term_with_score(self):
        """lookup_ontology_term surfaces the OLS score and respects ontology."""
        responses.add(
            responses.GET,
            "https://www.ebi.ac.uk/ols4/api/search",
            json={
                "response": {
                    "numFound": 1,
                    "docs": [
                        {
                            "iri": "http://www.ebi.ac.uk/efo/EFO_0000311",
                            "label": "cancer",
                            "short_form": "EFO_0000311",
                            "score": 14.2,
                        }
                    ],
                }
            },
            status=200,
        )

        result = lookup_ontology_term("cancer", "efo")
        assert result["@id"] == "http://www.ebi.ac.uk/efo/EFO_0000311"
        assert result["termCode"] == "EFO_0000311"
        assert result["score"] == 14.2
        # The request must carry the requested ontology and a rows param.
        sent = responses.calls[0].request
        assert sent.url is not None
        assert "ontology=efo" in sent.url
        assert "rows=" in sent.url

    @responses.activate
    def test_unit_resolves_to_uo_iri(self):
        """lookup_unit resolves to a UO IRI via the same OLS endpoint."""
        responses.add(
            responses.GET,
            "https://www.ebi.ac.uk/ols4/api/search",
            json={
                "response": {
                    "numFound": 1,
                    "docs": [
                        {
                            "iri": "http://purl.obolibrary.org/obo/UO_0000064",
                            "label": "micromolar",
                            "short_form": "UO_0000064",
                            "score": 8.0,
                        }
                    ],
                }
            },
            status=200,
        )

        result = lookup_unit("micromolar")
        assert result["@id"] == "http://purl.obolibrary.org/obo/UO_0000064"
        assert result["termCode"] == "UO_0000064"
        sent_url = responses.calls[0].request.url
        assert sent_url is not None
        assert "ontology=uo" in sent_url

    @responses.activate
    def test_bao_wrapper_still_pins_bao(self):
        """The back-compat lookup_bao_term wrapper still pins ontology=bao."""
        responses.add(
            responses.GET,
            "https://www.ebi.ac.uk/ols4/api/search",
            json=_load("bao_cell_viability.json"),
            status=200,
        )

        result = lookup_bao_term("cell viability assay")
        assert result["termCode"] == "BAO_0003009"
        sent_url = responses.calls[0].request.url
        assert sent_url is not None
        assert "ontology=bao" in sent_url


# ===========================================================================
# CompTox DTXSID (#146)
# ===========================================================================


class TestCompToxContract:
    """Contract tests for lookups/comptox.py."""

    @responses.activate
    def test_resolves_dtxsid_from_list(self):
        """A list response yields the first hit's DTXSID."""
        responses.add(
            responses.GET,
            "https://comptox.epa.gov/dashboard-api/ccdapp2/search/chemical/equal/Bisphenol%20A",
            json=[
                {
                    "dtxsid": "DTXSID7020182",
                    "preferredName": "Bisphenol A",
                    "casrn": "80-05-7",
                    "inchikey": "IISBACLAFKSPIT-UHFFFAOYSA-N",
                }
            ],
            status=200,
        )

        result = lookup_dtxsid("Bisphenol A")
        assert result["dtxsid"] == "DTXSID7020182"
        assert result["@type"] == "MolecularEntity"
        assert result["casrn"] == "80-05-7"
        assert result["inchikey"] == "IISBACLAFKSPIT-UHFFFAOYSA-N"
        assert result["@id"].endswith("DTXSID7020182")

    @responses.activate
    def test_no_hits_returns_empty(self):
        responses.add(
            responses.GET,
            "https://comptox.epa.gov/dashboard-api/ccdapp2/search/chemical/equal/Nope",
            json=[],
            status=200,
        )
        assert lookup_dtxsid("Nope") == {}

    @responses.activate
    def test_404_returns_empty(self):
        responses.add(
            responses.GET,
            "https://comptox.epa.gov/dashboard-api/ccdapp2/search/chemical/equal/Nope",
            status=404,
        )
        assert lookup_dtxsid("Nope") == {}

    @responses.activate
    def test_timeout_raises_transient(self):
        responses.add(
            responses.GET,
            "https://comptox.epa.gov/dashboard-api/ccdapp2/search/chemical/equal/BPA",
            body=Timeout("timed out"),
        )
        with pytest.raises(TransientLookupError):
            lookup_dtxsid("BPA")

    @responses.activate
    def test_empty_query_no_http(self):
        assert lookup_dtxsid("") == {}


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
# ORCID expanded-search (lookup_orcid_by_name)
# ===========================================================================


class TestORCIDByNameContract:
    """Contract tests for lookups.orcid.lookup_orcid_by_name (#180)."""

    @pytest.fixture(autouse=True)
    def _clear(self):
        from lookups.orcid import lookup_orcid_by_name

        lookup_orcid_by_name.cache_clear()  # ty: ignore[unresolved-attribute]
        yield

    def _result(self, *entries: dict) -> dict:
        return {"expanded-result": list(entries), "num-found": len(entries)}

    @responses.activate
    def test_single_match_parses_candidate(self):
        """One expanded-result row → one ranked candidate dict."""
        from lookups.orcid import lookup_orcid_by_name

        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/expanded-search/",
            json=self._result(
                {
                    "orcid-id": "0000-0003-4766-7358",
                    "given-names": "Fabian",
                    "family-names": "Wagenaars",
                    "institution-name": ["Utrecht University"],
                }
            ),
            status=200,
        )

        candidates = lookup_orcid_by_name("Fabian", "Wagenaars")

        assert candidates == [
            {
                "orcid": "0000-0003-4766-7358",
                "given": "Fabian",
                "family": "Wagenaars",
                "affiliation": "Utrecht University",
            }
        ]

    @responses.activate
    def test_multiple_matches_returns_all(self):
        """Several rows → all candidates, order preserved."""
        from lookups.orcid import lookup_orcid_by_name

        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/expanded-search/",
            json=self._result(
                {
                    "orcid-id": "0000-0001-1111-1111",
                    "given-names": "Jane",
                    "family-names": "Smith",
                    "institution-name": ["University A"],
                },
                {
                    "orcid-id": "0000-0002-2222-2222",
                    "given-names": "Jane",
                    "family-names": "Smith",
                    "institution-name": [],
                },
            ),
            status=200,
        )

        candidates = lookup_orcid_by_name("Jane", "Smith")

        assert [c["orcid"] for c in candidates] == [
            "0000-0001-1111-1111",
            "0000-0002-2222-2222",
        ]
        assert candidates[1]["affiliation"] == ""

    @responses.activate
    def test_no_matches_returns_empty_list(self):
        """Empty expanded-result → []."""
        from lookups.orcid import lookup_orcid_by_name

        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/expanded-search/",
            json=self._result(),
            status=200,
        )

        assert lookup_orcid_by_name("Nemo", "Nobody") == []

    @responses.activate
    def test_empty_family_no_http_returns_empty(self):
        """A blank family name short-circuits with no HTTP call."""
        from lookups.orcid import lookup_orcid_by_name

        assert lookup_orcid_by_name("Given", "") == []
        assert len(responses.calls) == 0

    @responses.activate
    def test_timeout_raises_transient(self):
        """A timeout raises TransientLookupError (not a silent empty)."""
        from lookups.orcid import lookup_orcid_by_name

        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/expanded-search/",
            body=Timeout("timed out"),
        )

        with pytest.raises(TransientLookupError):
            lookup_orcid_by_name("Fabian", "Wagenaars")


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

    @responses.activate
    def test_title_search_returns_candidates(self):
        """A bibliographic title search returns ranked {title, doi, score} dicts."""
        body = {
            "status": "ok",
            "message": {
                "items": [
                    {
                        "DOI": "10.1016/j.tox.2021.152898",
                        "title": ["A nephrotoxicity study in vitro"],
                        "score": 87.5,
                    },
                    {
                        "DOI": "10.9999/other",
                        "title": ["An unrelated paper"],
                        "score": 12.0,
                    },
                ]
            },
        }
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=body,
            status=200,
        )

        candidates = search_works_by_title("A nephrotoxicity study in vitro")

        assert len(candidates) == 2
        top = candidates[0]
        assert top["doi"] == "10.1016/j.tox.2021.152898"
        assert top["title"] == "A nephrotoxicity study in vitro"
        assert top["score"] == 87.5

    @responses.activate
    def test_title_search_no_items_returns_empty(self):
        """An empty Crossref result set yields an empty candidate list."""
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json={"status": "ok", "message": {"items": []}},
            status=200,
        )
        assert list(search_works_by_title("No such paper anywhere")) == []

    @responses.activate
    def test_title_search_blank_title_returns_empty(self):
        """A blank title is a no-op (no request, no candidates)."""
        assert list(search_works_by_title("   ")) == []
