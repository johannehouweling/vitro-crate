"""Concurrency tests for verify_all_identifiers (#62).

`verify_all_identifiers` walks every entity/field and calls a blocking network
lookup per field. The fields are independent, so they are verified with a
bounded ThreadPoolExecutor. This module asserts:

  * per-field verifications run concurrently (calls overlap in time),
  * results are identical to the serial path and order-independent,
  * every entity's field-status mutation still lands correctly.
"""

from __future__ import annotations

import threading

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools import verification


def _entity(state: CrateState, entity_id: str) -> Entity:
    """Fetch an entity, asserting it exists (keeps the type checker happy)."""
    ent = state.get_entity(entity_id)
    assert ent is not None, f"entity {entity_id} missing"
    return ent


def _field_status(state: CrateState, entity_id: str, field: str) -> str:
    """Return the completion status string for a field, asserting it is set."""
    fc = _entity(state, entity_id).get_field_status(field)
    assert fc is not None, f"no completion status for {entity_id}.{field}"
    return fc.status


def _make_state(n_compounds: int) -> CrateState:
    state = CrateState()
    for i in range(n_compounds):
        chem = Entity(
            entity_id=f"chem_{i:03d}",
            type="MolecularEntity",
            fields={"identifier": f"50-00-{i}"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.set_field_status("identifier", "filled", "llm")
        state.add_entity(chem)
    return state


class TestVerifyAllConcurrency:
    """verify_all_identifiers verifies independent fields concurrently."""

    def test_results_identical_and_order_independent(self, monkeypatch):
        """Concurrent verification yields one verified result per filled field."""
        state = _make_state(5)

        def fake_lookup(query):
            return {"found": True, "data": {"pubchem_cid": "712"}, "error": None}

        monkeypatch.setattr(verification, "lookup_compound", fake_lookup)

        results = verification.verify_all_identifiers(state)

        # One result per compound, all verified, no duplicates/drops.
        assert len(results) == 5
        assert all(r["verified"] for r in results)
        ids = {r["entity_id"] for r in results}
        assert ids == {f"chem_{i:03d}" for i in range(5)}
        # Mutations landed: every entity field is now "verified".
        for i in range(5):
            assert _field_status(state, f"chem_{i:03d}", "identifier") == "verified"

    def test_verifications_run_concurrently(self, monkeypatch):
        """The independent per-field lookups genuinely overlap.

        Proved by *rendezvous*, not by elapsed wall-clock (#406). A barrier of
        ``k`` parties cannot be crossed unless ``k`` lookups are in flight at the
        same moment, so crossing it IS the concurrency claim — and it holds no
        matter how the scheduler treats these threads.

        The old version asserted ``elapsed < 0.9`` against ``time.sleep(0.2)``
        per lookup. That is an *upper* bound on wall-clock, which a loaded
        machine violates while the code under test is perfectly correct; it made
        this one of the four tests that failed under full-suite ``-n auto``.
        Loosening the threshold would have been the tempting fix and the wrong
        one — it weakens the test without making it deterministic.

        Sizing the barrier from ``_VERIFY_WORKERS`` also strengthens the claim:
        the old assertion settled for ``max_active >= 2``, this one pins the
        full configured pool width, and it self-adjusts if that width changes.
        """
        n = 6
        state = _make_state(n)
        parties = min(verification._VERIFY_WORKERS, n)
        # Guard the guard: sizing the barrier from the value under test would go
        # vacuous if that value ever became 1 — a one-party barrier is crossed by
        # serial code. Verified by mutation: without this line, setting
        # _VERIFY_WORKERS = 1 makes a serial pool pass this test.
        assert parties >= 2, (
            f"_VERIFY_WORKERS={verification._VERIFY_WORKERS} configures a serial "
            f"pool, so verify_all_identifiers cannot be concurrent at all"
        )
        # A generous timeout: it is never waited out on success (the barrier
        # releases the instant the last party arrives), and on failure it is a
        # definite BrokenBarrierError rather than a flaky threshold.
        barrier = threading.Barrier(parties, timeout=20)
        max_active = 0
        active = 0
        lock = threading.Lock()

        def rendezvous_lookup(query):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:  # pragma: no cover - failure path
                raise AssertionError(
                    f"only {max_active} lookup(s) ever ran at once; "
                    f"{parties} must overlap for the pool to be concurrent"
                ) from None
            finally:
                with lock:
                    active -= 1
            return {"found": True, "data": {"pubchem_cid": "712"}, "error": None}

        monkeypatch.setattr(verification, "lookup_compound", rendezvous_lookup)

        results = verification.verify_all_identifiers(state)

        assert len(results) == n
        assert max_active >= parties, f"expected {parties} overlapping lookups, saw {max_active}"

    def test_matches_serial_outcome_for_mixed_results(self, monkeypatch):
        """A mix of verified/cleared fields matches what the serial path would do."""
        state = CrateState()
        good = Entity(
            entity_id="chem_good",
            type="MolecularEntity",
            fields={"identifier": "50-00-0"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        good.set_field_status("identifier", "filled", "llm")
        state.add_entity(good)

        bad = Entity(
            entity_id="chem_bad",
            type="MolecularEntity",
            fields={"identifier": "not-real"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        bad.set_field_status("identifier", "filled", "llm")
        state.add_entity(bad)

        def fake_lookup(query):
            if query == "50-00-0":
                return {"found": True, "data": {"pubchem_cid": "712"}, "error": None}
            return {"found": False, "data": {}, "error": "not found"}

        monkeypatch.setattr(verification, "lookup_compound", fake_lookup)

        results = verification.verify_all_identifiers(state)

        by_id = {r["entity_id"]: r for r in results}
        assert by_id["chem_good"]["verified"] is True
        assert by_id["chem_bad"]["verified"] is False
        # Good kept + marked verified; bad cleared + marked missing.
        assert _field_status(state, "chem_good", "identifier") == "verified"
        assert "identifier" not in _entity(state, "chem_bad").fields
        assert _field_status(state, "chem_bad", "identifier") == "missing"
