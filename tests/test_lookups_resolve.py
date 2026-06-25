"""Performance + caching tests for compound resolution (Issue #252).

``resolve_compound`` was very slow for some compounds (30-66s each) under a
concurrent burst: a single call fanned out to up to SIX PubChem round-trips
(name->JSON + synonyms for the lookup, then a fresh re-resolution for each of
the CAS and PubChem-CID verifications), and a 429 storm multiplied the
retry/backoff across all of them.

These tests pin the levers added to close that gap, all offline (the HTTP layer
is mocked):

* an in-process resolution cache keyed by *normalized* name so a repeat name —
  even with different casing/whitespace — issues no second network call;
* warming that cache with the resolved CAS / CID alias keys so the verify step
  reuses the already-fetched authoritative record instead of re-resolving the
  compound twice (collapsing 6 round-trips to ~2);
* a bounded per-compound timeout that returns a graceful partial/empty result
  rather than hanging ~60s;
* a client-side concurrency throttle so concurrent resolves do not all storm
  PubChem at once.
"""

from __future__ import annotations

import threading
import time

import pytest

from builder.state import CrateState
from builder.tools import composites
from builder.tools import lookups as facade
from builder.tools._resolve_cache import (
    compound_cache,
    normalize_compound_name,
    resolve_concurrency,
    run_with_timeout,
)

pytestmark = pytest.mark.timeout(30)


_PUBCHEM_HIT = {
    "cas": "13292-46-1",
    "smiles": "C[C@H]1...",
    "inchikey": "JQXXHWHPUNPDRT-WLSIYKJHSA-N",
    "inchi": "InChI=1S/...",
    "formula": "C43H58N4O12",
    "mass": "822.9",
    "iupac_name": "rifampicin",
    "pubchem_cid": "5360545",
}


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset module caches before and after each test."""
    facade.lookup_compound.cache_clear()
    compound_cache.clear()
    resolve_concurrency.reset()
    yield
    facade.lookup_compound.cache_clear()
    compound_cache.clear()
    resolve_concurrency.reset()


def _always_verify(state, entity_id, field):  # noqa: ANN001
    """Stand-in verifier that mimics verify_identifier's success path."""
    entity = state.get_entity(entity_id)
    if entity is not None and entity.fields.get(field):
        entity.set_field_status(field, "verified", "lookup")
        return {
            "verified": True,
            "entity_id": entity_id,
            "field": field,
            "message": f"Verified {field}",
            "suggested_fix": None,
        }
    return {
        "verified": False,
        "entity_id": entity_id,
        "field": field,
        "message": f"No value for {field}",
        "suggested_fix": None,
    }


class TestNormalize:
    def test_strips_and_casefolds(self):
        assert normalize_compound_name("  Rifampicin ") == normalize_compound_name(
            "rifampicin"
        )

    def test_collapses_internal_whitespace(self):
        assert normalize_compound_name("Silychristin   A") == normalize_compound_name(
            "silychristin a"
        )


class TestResolveCompoundCache:
    def test_repeat_name_hits_cache_no_second_http(self, monkeypatch):
        """Two resolves of the same normalized name => ONE underlying lookup."""
        calls: list[str] = []

        def fake_lookup(name):  # noqa: ANN001
            calls.append(name)
            return {"found": True, "data": dict(_PUBCHEM_HIT), "error": None}

        monkeypatch.setattr(composites, "lookup_compound", fake_lookup)
        monkeypatch.setattr(composites, "verify_identifier", _always_verify)

        state = CrateState()
        r1 = composites.resolve_compound(state, name="Rifampicin")
        # Different casing/whitespace must still hit the cache.
        r2 = composites.resolve_compound(state, name="  rifampicin ")

        assert r1["entity_id"] == r2["entity_id"]
        assert len(calls) == 1, f"expected one lookup, got {calls}"


class TestResolveVerifyReusesCache:
    def test_verify_reuses_warmed_cache(self, monkeypatch):
        """The CAS/CID verify step must not trigger fresh PubChem round-trips.

        ``resolve_compound`` warms the in-process cache with the resolved CAS and
        ``CID <cid>`` alias keys, so the real ``verify_identifier`` (which
        re-resolves via ``lookup_compound``) reads the cache instead of the
        network. We assert the underlying PubChem client is invoked at most once
        across the whole resolve (the initial name resolution), proving the two
        verify lookups were served from cache.
        """
        net_calls: list[str] = []

        def fake_pubchem(name):  # noqa: ANN001
            net_calls.append(name)
            # Echo the same record regardless of whether queried by name/CAS/CID.
            return dict(_PUBCHEM_HIT)

        monkeypatch.setattr("builder.tools.lookups.lookup_pubchem", fake_pubchem)
        # Keep ChEBI fallback offline (never reached on a hit, but be safe).
        monkeypatch.setattr(
            "builder.tools.lookups.lookup_ontology_term_ols", lambda raw, ont: {}
        )

        state = CrateState()
        result = composites.resolve_compound(state, name="Rifampicin")

        assert result["verified"] is True
        # The name resolution is the only network round-trip; both the CAS and
        # the CID verify lookups were served from the warmed in-process cache.
        assert len(net_calls) == 1, f"verify re-hit the network: {net_calls}"


class TestResolveTimeout:
    def test_timeout_yields_graceful_result_not_hang(self, monkeypatch):
        """A lookup that would hang must be bounded; resolve returns gracefully."""

        def slow_lookup(name):  # noqa: ANN001
            time.sleep(30)  # would blow the 30s test budget if not bounded
            return {"found": True, "data": dict(_PUBCHEM_HIT), "error": None}

        monkeypatch.setattr(composites, "lookup_compound", slow_lookup)
        monkeypatch.setattr(composites, "verify_identifier", _always_verify)

        state = CrateState()
        start = time.monotonic()
        result = composites.resolve_compound(state, name="Rifampicin", timeout=0.2)
        elapsed = time.monotonic() - start

        assert elapsed < 5, f"resolve hung instead of timing out: {elapsed:.1f}s"
        assert result.get("ok") is False
        assert "timeout" in (result.get("error", "").lower())
        # No fabricated entity on a timeout.
        assert [e for e in state.list_entities() if e.type == "MolecularEntity"] == []


class TestRunWithTimeout:
    def test_returns_value_when_fast(self):
        ok, value = run_with_timeout(lambda: 42, timeout=1.0)
        assert ok is True
        assert value == 42

    def test_signals_timeout_when_slow(self):
        ok, value = run_with_timeout(lambda: time.sleep(5), timeout=0.1)
        assert ok is False
        assert value is None


class TestResolveConcurrencyThrottle:
    def test_caps_simultaneous_resolutions(self):
        """The shared throttle bounds how many resolves run PubChem at once."""
        throttle = resolve_concurrency
        max_observed = 0
        current = 0
        lock = threading.Lock()
        n = 8

        def worker() -> None:
            nonlocal current, max_observed
            with throttle.slot():
                with lock:
                    current += 1
                    max_observed = max(max_observed, current)
                time.sleep(0.05)
                with lock:
                    current -= 1

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_observed <= throttle.limit, (
            f"throttle let {max_observed} run at once (limit {throttle.limit})"
        )
