"""Recall tests for ``lookups.cellosaurus.search_cellosaurus`` (#385).

A single Solr query against the combined ``idsy`` field at ``rows=10`` ranks the
*parent* cell line far outside the requested window for most common in-vitro
toxicology names: Cellosaurus is dominated by engineered derivatives whose
primary identifier contains the parent's name as a token. ``CHO-K1`` (CVCL_0214)
sits at rank 488 of 1116 for ``idsy:"CHO-K1"``; ``HepG2`` (CVCL_0027) at 53 of 88.
The D5 exact-match gate in :func:`builder.tools.lookups.lookup_cell_line_by_name`
then correctly reports "no confident match" over a candidate list that never
contained the right answer.

Every test here replays payloads recorded verbatim from
``api.cellosaurus.org/search/cell-line`` (``fields=id,ac,sy``, ``format=json``,
2026-07-27), routed by the ``q`` query parameter, and drives the real entry
point. Fully offline. The ranking *is* the fact under test, so the fixtures are
not trimmed or reordered. Cellosaurus ranking is not a contract: if it re-ranks,
these stay green while the live behaviour regresses — ``tests/test_lookups_live.py``
is the only guard against that, and it is opt-in.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import responses
from requests.exceptions import Timeout
from responses import matchers

from builder.tools.lookups import lookup_cell_line_by_name
from lookups._http import reset_host_throttle
from lookups.cellosaurus import search_cellosaurus

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://api.cellosaurus.org/search/cell-line"
EMPTY_RESULT = {"Cellosaurus": {"cell-line-list": []}}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _route(query: str, **kwargs) -> None:
    """Register a search response for exactly one Solr ``q`` value."""
    responses.add(
        responses.GET,
        SEARCH_URL,
        match=[matchers.query_param_matcher({"q": query}, strict_match=False)],
        **kwargs,
    )


def _sent_params(param: str) -> list[str]:
    return [
        parse_qs(urlsplit(call.request.url or "").query)[param][0] for call in responses.calls
    ]


def _sent_queries() -> list[str]:
    return _sent_params("q")


def _sent_rows() -> list[int]:
    return [int(rows) for rows in _sent_params("rows")]


@pytest.fixture(autouse=True)
def _clear_caches():
    """Both the client and its D5 wrapper memoize; clear before and after."""
    search_cellosaurus.cache_clear()
    lookup_cell_line_by_name.cache_clear()
    reset_host_throttle()
    yield
    search_cellosaurus.cache_clear()
    lookup_cell_line_by_name.cache_clear()


class TestCellosaurusNameSearchRecall:
    """The candidate list must contain the parent entry for common names."""

    @responses.activate
    def test_chok1_resolves_to_cvcl_0214_from_recorded_payloads(self):
        """CHO-K1 → CVCL_0214, selected by the untouched D5 gate over the union.

        The legacy combined-field route is registered too, with the genuine
        ``idsy:"CHO-K1"&rows=10`` body, so a client that queries it fails here
        with the real "no confident match" outcome rather than a routing error.
        """
        _route('idsy:"CHO-K1"', json=_load("cellosaurus_search_idsy_chok1_rows10.json"))
        _route('id:"CHO-K1"', json=_load("cellosaurus_search_id_chok1.json"))
        _route('sy:"CHO-K1"', json=_load("cellosaurus_search_sy_chok1.json"))

        result = lookup_cell_line_by_name("CHO-K1")

        assert result["found"] is True
        assert result["data"]["accession"] == "CVCL_0214"
        assert result["data"]["name"] == "CHO-K1"

    @responses.activate
    def test_chok1_unresolvable_without_the_identifier_route(self):
        """Honesty control: the synonym route alone cannot find CVCL_0214."""
        _route('id:"CHO-K1"', json=EMPTY_RESULT)
        _route('sy:"CHO-K1"', json=_load("cellosaurus_search_sy_chok1.json"))

        result = lookup_cell_line_by_name("CHO-K1")

        assert set(_sent_queries()) == {'id:"CHO-K1"', 'sy:"CHO-K1"'}
        assert result["found"] is False
        assert result["data"] == {}

    @responses.activate
    def test_hepg2_resolves_via_the_synonym_route(self):
        """HepG2 is a *synonym* of CVCL_0027 — its primary identifier is ``Hep-G2``.

        As above, the genuine ``idsy:"HepG2"&rows=10`` body backs the legacy
        route so the pre-#385 client reddens on the resolution outcome.
        """
        _route('idsy:"HepG2"', json=_load("cellosaurus_search_idsy_hepg2_rows10.json"))
        _route('id:"HepG2"', json=_load("cellosaurus_search_id_hepg2.json"))
        _route('sy:"HepG2"', json=_load("cellosaurus_search_sy_hepg2.json"))

        result = lookup_cell_line_by_name("HepG2")

        assert result["found"] is True
        assert result["data"]["accession"] == "CVCL_0027"
        assert result["data"]["name"] == "Hep-G2"

    @responses.activate
    def test_hepg2_unresolvable_without_the_synonym_route(self):
        """Honesty control: ``id:"HepG2"`` never returns CVCL_0027 at any row count."""
        _route('id:"HepG2"', json=_load("cellosaurus_search_id_hepg2.json"))
        _route('sy:"HepG2"', json=EMPTY_RESULT)

        result = lookup_cell_line_by_name("HepG2")

        assert set(_sent_queries()) == {'id:"HepG2"', 'sy:"HepG2"'}
        assert result["found"] is False
        assert result["data"] == {}

    @responses.activate
    def test_hepg2_unresolvable_when_only_ten_synonym_hits_are_returned(self):
        """CVCL_0027 ranks 21st of 45 on ``sy:"HepG2"`` — a 10-row window misses it.

        The synonym body here is the genuine ``rows=10`` server response, not a
        truncation of the 45-hit one.
        """
        _route('id:"HepG2"', json=_load("cellosaurus_search_id_hepg2.json"))
        _route('sy:"HepG2"', json=_load("cellosaurus_search_sy_hepg2_rows10.json"))

        result = lookup_cell_line_by_name("HepG2")

        assert set(_sent_queries()) == {'id:"HepG2"', 'sy:"HepG2"'}
        assert result["found"] is False

    @responses.activate
    def test_hepg2_resolution_depends_on_the_widened_row_count(self):
        """A server honouring the client's ``rows`` resolves HepG2 only above 10.

        Unlike the fixed-body tests above, this route answers with whichever
        recorded body the requested ``rows`` would really have produced, so it
        reddens if the per-field row count is narrowed back toward 10.
        """
        wide = _load("cellosaurus_search_sy_hepg2.json")
        narrow = _load("cellosaurus_search_sy_hepg2_rows10.json")

        def _by_rows(request):
            rows = int(parse_qs(urlsplit(request.url).query)["rows"][0])
            body = narrow if rows <= 10 else wide
            return 200, {"Content-Type": "application/json"}, json.dumps(body)

        _route('id:"HepG2"', json=_load("cellosaurus_search_id_hepg2.json"))
        responses.add_callback(
            responses.GET,
            SEARCH_URL,
            callback=_by_rows,
            content_type="application/json",
            match=[matchers.query_param_matcher({"q": 'sy:"HepG2"'}, strict_match=False)],
        )

        result = lookup_cell_line_by_name("HepG2")

        assert result["found"] is True
        assert result["data"]["accession"] == "CVCL_0027"

    @responses.activate
    def test_search_requests_at_least_25_rows_per_field(self):
        """Read off the request the client built: 10 rows per field is not enough."""
        _route('id:"CHO-K1"', json=_load("cellosaurus_search_id_chok1.json"))
        _route('sy:"CHO-K1"', json=_load("cellosaurus_search_sy_chok1.json"))

        search_cellosaurus("CHO-K1")

        assert _sent_rows()
        assert all(rows >= 25 for rows in _sent_rows())

    @responses.activate
    def test_search_issues_exactly_two_requests_one_per_name_field(self):
        """A single combined ``id: OR sy:`` query degrades to the broken ranking.

        Measured 2026-07-25: ``id:"CHO-K1" OR sy:"CHO-K1"`` leaves CVCL_0214
        outside a 10-row window and ``id:"HepG2" OR sy:"HepG2"`` leaves CVCL_0027
        outside a 50-row one. Two requests are load-bearing.
        """
        _route('id:"CHO-K1"', json=_load("cellosaurus_search_id_chok1.json"))
        _route('sy:"CHO-K1"', json=_load("cellosaurus_search_sy_chok1.json"))

        search_cellosaurus("CHO-K1")

        assert len(responses.calls) == 2
        assert set(_sent_queries()) == {'id:"CHO-K1"', 'sy:"CHO-K1"'}

    @responses.activate
    def test_union_dedupes_by_accession(self):
        """CVCL_0265 is in both FRTL-5 payloads; a naive concat makes it ambiguous."""
        _route('id:"FRTL-5"', json=_load("cellosaurus_search_id_frtl5.json"))
        _route('sy:"FRTL-5"', json=_load("cellosaurus_search_sy_frtl5.json"))

        accessions = [c["accession"] for c in search_cellosaurus("FRTL-5")]

        assert accessions.count("CVCL_0265") == 1
        assert lookup_cell_line_by_name("FRTL-5")["found"] is True

    @responses.activate
    def test_transient_on_one_route_never_yields_a_partial_union(self):
        """A half-fetched candidate list must never mint an accession (D5)."""
        _route('id:"CHO-K1"', json=_load("cellosaurus_search_id_chok1.json"))
        _route('sy:"CHO-K1"', body=Timeout("timed out"))

        result = lookup_cell_line_by_name("CHO-K1")

        assert set(_sent_queries()) == {'id:"CHO-K1"', 'sy:"CHO-K1"'}
        assert result["found"] is False
        assert result["transient"] is True

    @responses.activate
    def test_d5_gate_still_rejects_two_exact_matches_across_the_wider_list(self):
        """Widening the candidate list must not loosen the exact+unique gate."""
        _route(
            'id:"AmbiguousLine"',
            json={
                "Cellosaurus": {
                    "cell-line-list": [
                        {
                            "accession-list": [{"type": "primary", "value": "CVCL_AAA1"}],
                            "name-list": [{"type": "identifier", "value": "AmbiguousLine"}],
                        }
                    ]
                }
            },
        )
        _route(
            'sy:"AmbiguousLine"',
            json={
                "Cellosaurus": {
                    "cell-line-list": [
                        {
                            "accession-list": [{"type": "primary", "value": "CVCL_BBB2"}],
                            "name-list": [{"type": "identifier", "value": "AmbiguousLine"}],
                        }
                    ]
                }
            },
        )

        result = lookup_cell_line_by_name("AmbiguousLine")

        assert result["found"] is False
        assert "CVCL_AAA1" in result["error"]
        assert "CVCL_BBB2" in result["error"]
