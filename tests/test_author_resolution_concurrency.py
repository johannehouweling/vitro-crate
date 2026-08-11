"""Authors are verified against ORCID together, not one after another.

`draft_publication_with_authors` walks each author through a cascade, and its
first step is a network verification of the ORCID Crossref supplied. Done in the
loop, that is one round-trip per author, strictly in series — and any one of them
landing on the HTTP retry ladder (10s timeout, up to four attempts) stalls every
author behind it. A profiled run spent **405 seconds inside a single call** for one
DOI: 22% of that whole session's machine time.

Only that step moves. The in-crate match and the person entities write to
CrateState, and the name-search branch can ask the human; both stay on the calling
thread, in author order. These tests pin that split.
"""

from __future__ import annotations

import threading
import time

import pytest

from builder.tools.composites import (
    _AUTHOR_VERIFY_TIMEOUT,
    _prefetch_orcid_verifications,
)


def _author(family, orcid=None):
    out = {"givenName": "A", "familyName": family}
    if orcid:
        out["identifier"] = orcid
    return out


def _orcid_ok(family):
    return {"found": True, "data": {"familyName": family, "givenName": "A"}}


class TestItRunsThemTogether:
    def test_slow_lookups_overlap(self):
        """Six 0.4s lookups in series would be 2.4s. Concurrently, far less."""
        authors = [_author(f"Name{i}", f"0000-0000-0000-000{i}") for i in range(6)]

        def slow(orcid_id):
            time.sleep(0.4)
            return _orcid_ok(f"Name{orcid_id[-1]}")

        started = time.monotonic()
        out = _prefetch_orcid_verifications(authors, slow)
        elapsed = time.monotonic() - started

        assert len(out) == 6
        assert all(v is not None for v in out.values())
        assert elapsed < 1.6, f"{elapsed:.2f}s — looks serial"

    def test_the_concurrency_gate_is_respected(self):
        """Shared with resolve_compound, so ORCID is not hammered either."""
        from builder.tools._resolve_cache import resolve_concurrency

        peak = 0
        live = 0
        lock = threading.Lock()

        def watched(orcid_id):
            nonlocal peak, live
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1
            return _orcid_ok("X")

        authors = [_author("X", f"0000-0000-0000-{i:04d}") for i in range(12)]
        _prefetch_orcid_verifications(authors, watched)
        assert peak <= resolve_concurrency.limit


class TestItKeepsTheCascadeHonest:
    def test_results_are_keyed_by_author_index(self):
        authors = [_author("Alpha", "0000-0000-0000-0001"), _author("Beta", "0000-0000-0000-0002")]
        out = _prefetch_orcid_verifications(
            authors, lambda oid: _orcid_ok("Alpha" if oid.endswith("1") else "Beta")
        )
        first, second = out[0], out[1]
        assert first is not None and second is not None
        assert first["familyName"] == "Alpha"
        assert second["familyName"] == "Beta"

    def test_an_author_without_a_crossref_orcid_is_not_attempted(self):
        authors = [_author("Alpha"), _author("Beta", "0000-0000-0000-0002")]
        out = _prefetch_orcid_verifications(authors, lambda oid: _orcid_ok("Beta"))
        assert 0 not in out, "nothing to verify, so nothing should be recorded"
        assert out[1] is not None

    def test_a_mismatched_family_name_is_not_verified(self):
        """D5 — an ORCID is only trusted when the resolved name matches."""
        authors = [_author("Alpha", "0000-0000-0000-0001")]
        out = _prefetch_orcid_verifications(authors, lambda oid: _orcid_ok("Somebody Else"))
        assert out[0] is None

    def test_nothing_to_do_costs_nothing(self):
        assert _prefetch_orcid_verifications([], lambda oid: _orcid_ok("X")) == {}
        assert _prefetch_orcid_verifications([_author("Alpha")], None) == {}


class TestOneSlowLookupCannotHoldThePaper:
    def test_a_hung_verification_times_out_to_none(self):
        """None is what the cascade already reads as "not verified"."""
        authors = [_author("Alpha", "0000-0000-0000-0001")]

        def hangs(orcid_id):
            time.sleep(_AUTHOR_VERIFY_TIMEOUT + 5)
            return _orcid_ok("Alpha")

        import builder.tools.composites as composites

        original = composites._AUTHOR_VERIFY_TIMEOUT
        composites._AUTHOR_VERIFY_TIMEOUT = 0.2
        try:
            started = time.monotonic()
            out = _prefetch_orcid_verifications(authors, hangs)
            elapsed = time.monotonic() - started
        finally:
            composites._AUTHOR_VERIFY_TIMEOUT = original

        assert out[0] is None
        assert elapsed < 2, f"{elapsed:.2f}s — the timeout did not bound it"

    def test_one_slow_author_does_not_block_the_others(self):
        authors = [_author(f"N{i}", f"0000-0000-0000-000{i}") for i in range(4)]

        def mixed(orcid_id):
            if orcid_id.endswith("0"):
                time.sleep(0.6)
            return _orcid_ok(f"N{orcid_id[-1]}")

        started = time.monotonic()
        out = _prefetch_orcid_verifications(authors, mixed)
        elapsed = time.monotonic() - started
        assert len(out) == 4
        assert elapsed < 1.2, f"{elapsed:.2f}s — the fast three waited on the slow one"


class TestAnErrorIsNotSwallowed:
    def test_a_lookup_that_raises_propagates(self):
        """A bug in the lookup layer must not be reported as an unverified author."""
        authors = [_author("Alpha", "0000-0000-0000-0001")]

        def boom(orcid_id):
            raise RuntimeError("lookup layer is broken")

        with pytest.raises(RuntimeError):
            _prefetch_orcid_verifications(authors, boom)
