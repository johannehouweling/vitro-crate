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
import time

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
        """The independent per-field lookups overlap in time."""
        state = _make_state(6)
        active = 0
        max_active = 0
        lock = threading.Lock()

        def slow_lookup(query):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.2)
                return {"found": True, "data": {"pubchem_cid": "712"}, "error": None}
            finally:
                with lock:
                    active -= 1

        monkeypatch.setattr(verification, "lookup_compound", slow_lookup)

        start = time.monotonic()
        results = verification.verify_all_identifiers(state)
        elapsed = time.monotonic() - start

        assert len(results) == 6
        # Serial would be 6 * 0.2 = 1.2s; concurrent must be well under that.
        assert elapsed < 0.9, f"verifications not concurrent, elapsed={elapsed:.3f}s"
        assert max_active >= 2, f"expected overlapping lookups, max_active={max_active}"

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
