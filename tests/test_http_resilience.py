"""Tests for the shared resilient HTTP helper and lookup transient handling (#49)."""

from __future__ import annotations

import pytest
import responses
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import Timeout

from lookups._http import (
    NOT_FOUND,
    TransientLookupError,
    get_session,
    http_get_json,
)


class TestHttpGetJson:
    """http_get_json maps HTTP outcomes to data / NOT_FOUND / transient error."""

    @responses.activate
    def test_200_returns_parsed_json(self):
        responses.add(responses.GET, "https://api.test/x", json={"a": 1}, status=200)
        assert http_get_json("https://api.test/x") == {"a": 1}

    @responses.activate
    def test_404_returns_not_found(self):
        responses.add(responses.GET, "https://api.test/x", status=404)
        assert http_get_json("https://api.test/x") is NOT_FOUND

    @responses.activate
    def test_other_4xx_returns_not_found(self):
        responses.add(responses.GET, "https://api.test/x", status=400)
        assert http_get_json("https://api.test/x") is NOT_FOUND

    @responses.activate
    def test_503_raises_transient(self):
        responses.add(responses.GET, "https://api.test/x", status=503)
        with pytest.raises(TransientLookupError):
            http_get_json("https://api.test/x")

    @responses.activate
    def test_429_raises_transient(self):
        responses.add(responses.GET, "https://api.test/x", status=429)
        with pytest.raises(TransientLookupError):
            http_get_json("https://api.test/x")

    @responses.activate
    def test_timeout_raises_transient(self):
        responses.add(responses.GET, "https://api.test/x", body=Timeout("timed out"))
        with pytest.raises(TransientLookupError):
            http_get_json("https://api.test/x")

    @responses.activate
    def test_connection_error_raises_transient(self):
        responses.add(
            responses.GET, "https://api.test/x", body=ReqConnectionError("reset")
        )
        with pytest.raises(TransientLookupError):
            http_get_json("https://api.test/x")

    @responses.activate
    def test_malformed_json_raises_transient(self):
        responses.add(
            responses.GET,
            "https://api.test/x",
            body="not json",
            status=200,
            content_type="application/json",
        )
        with pytest.raises(TransientLookupError):
            http_get_json("https://api.test/x")

    @responses.activate
    def test_passes_params_and_headers(self):
        responses.add(
            responses.GET, "https://api.test/x", json={"ok": True}, status=200
        )
        http_get_json(
            "https://api.test/x",
            params={"q": "v"},
            headers={"Accept": "application/json"},
        )
        assert "q=v" in responses.calls[0].request.url


class TestSessionRetryConfig:
    """The shared Session retries transient statuses with backoff + Retry-After."""

    def test_retry_configuration(self):
        adapter = get_session().get_adapter("https://example.org")
        retry = adapter.max_retries
        assert retry.total == 3
        assert retry.backoff_factor == 0.5
        assert {429, 500, 502, 503, 504}.issubset(set(retry.status_forcelist))
        assert retry.respect_retry_after_header is True

    def test_session_is_shared(self):
        assert get_session() is get_session()


class TestNoNegativeCaching:
    """A transient failure must not be cached (lru_cache skips raised errors)."""

    def test_transient_then_success_rehits(self, monkeypatch):
        from lookups import crossref

        crossref.lookup_doi.cache_clear()
        calls = {"n": 0}

        def fake_get_json(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientLookupError("simulated outage")
            return {"message": {"title": ["A Title"], "type": "journal-article"}}

        # crossref imported http_get_json into its own namespace.
        monkeypatch.setattr(crossref, "http_get_json", fake_get_json)

        doi = "10.1/transient-x"
        with pytest.raises(TransientLookupError):
            crossref.lookup_doi(doi)

        # Same arg again: must re-invoke (not return a cached failure).
        result = crossref.lookup_doi(doi)
        assert result.get("name") == "A Title"
        assert calls["n"] == 2


class TestFacadeTransientMapping:
    """The facade maps transient errors to a distinct _failure error string."""

    @responses.activate
    def test_transient_maps_to_distinct_failure(self):
        from builder.tools import lookups as facade
        from lookups import crossref

        crossref.lookup_doi.cache_clear()
        facade.lookup_doi.cache_clear()
        doi = "10.2/transient-y"
        responses.add(responses.GET, f"{crossref._BASE}/{doi}", status=503)

        result = facade.lookup_doi(doi)
        assert result["found"] is False
        assert "transient" in result["error"].lower()

    @responses.activate
    def test_not_found_is_not_labelled_transient(self):
        from builder.tools import lookups as facade
        from lookups import crossref

        crossref.lookup_doi.cache_clear()
        facade.lookup_doi.cache_clear()
        doi = "10.3/missing-z"
        responses.add(responses.GET, f"{crossref._BASE}/{doi}", status=404)

        result = facade.lookup_doi(doi)
        assert result["found"] is False
        assert "transient" not in (result["error"] or "").lower()
