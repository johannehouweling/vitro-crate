"""Tests for the ``resolve_compound`` composite tool (Issue #179, task 3).

``resolve_compound`` fuses the recurring lookup -> draft -> verify sequence for a
chemical into ONE deterministic call: it resolves a compound name via
:func:`lookup_compound` (PubChem/ChEBI), mints the ``MolecularEntity`` via the
pure :func:`draft_molecular_entity` drafter (reusing the existing identifier-PV
wiring for CAS + PubChem CID), then confirms the minted identifiers against
source via :func:`verify_identifier`. D5: if verification fails, the identifier is
NOT fabricated/kept — the failure is surfaced in the return value.

Tests never hit the network: ``lookup_compound`` and ``verify_identifier`` are
monkeypatched on the ``composites`` module (where ``resolve_compound`` calls
them), so the chain runs entirely offline.
"""

from __future__ import annotations

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools import composites
from builder.tools.composites import resolve_compound

pytestmark = pytest.mark.timeout(120)


@pytest.fixture(autouse=True)
def _clear_resolve_caches():
    """Reset the shared in-process compound cache between tests (Issue #252).

    ``resolve_compound`` warms a process-wide cache; without this an earlier
    test's resolution could serve a later one from cache and starve its mock.
    """
    from builder.tools._resolve_cache import compound_cache, resolve_concurrency

    compound_cache.clear()
    resolve_concurrency.reset()
    yield
    compound_cache.clear()
    resolve_concurrency.reset()


def _by_type(state: CrateState, type_name: str) -> list:
    return [e for e in state.list_entities() if e.type == type_name]


_PUBCHEM_HIT = {
    "found": True,
    "error": None,
    "data": {
        "cas": "33889-69-9",
        "smiles": "C[C@H]1...",
        "inchikey": "ABCDEFGHIJKLMN-UHFFFAOYSA-N",
        "inchi": "InChI=1S/...",
        "formula": "C25H22O10",
        "mass": "482.4",
        "iupac_name": "silychristin A",
        "pubchem_cid": "443515",
    },
}


@pytest.fixture
def offline_lookup(monkeypatch):
    """Serve a fixed PubChem hit and a verifier that always confirms.

    Both are patched on the ``composites`` module namespace, which is where
    ``resolve_compound`` resolves the symbols (it imports them at module scope).
    """
    calls: dict[str, list] = {"lookup": [], "verify": []}

    def fake_lookup(name):  # noqa: ANN001
        calls["lookup"].append(name)
        return dict(_PUBCHEM_HIT)

    def fake_verify(state, entity_id, field):  # noqa: ANN001
        calls["verify"].append((entity_id, field))
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

    monkeypatch.setattr(composites, "lookup_compound", fake_lookup)
    monkeypatch.setattr(composites, "verify_identifier", fake_verify)
    return calls


class TestResolveCompoundHappyPath:
    def test_mints_verified_molecular_entity(self, offline_lookup):
        state = CrateState()
        result = resolve_compound(state, name="Silychristin A")

        mols = _by_type(state, "MolecularEntity")
        assert len(mols) == 1
        mol = mols[0]

        # The entity id is returned and the entity exists in state.
        assert result["entity_id"] == mol.entity_id
        assert state.get_entity(result["entity_id"]) is mol

        # CAS + PubChem CID land on the entity as the verifiable identifier
        # fields the build's _identifier_pv path turns into PropertyValues.
        assert mol.fields.get("cas") == "33889-69-9"
        assert mol.fields.get("pubchem_cid") == "443515"

    def test_reports_verification_verdict(self, offline_lookup):
        state = CrateState()
        result = resolve_compound(state, name="Silychristin A")

        assert result["verified"] is True
        # Both minted identifiers were verified against source.
        fields = {v["field"]: v["verified"] for v in result["verifications"]}
        assert fields.get("cas") is True
        assert fields.get("pubchem_cid") is True

    def test_returns_resolved_identifiers(self, offline_lookup):
        state = CrateState()
        result = resolve_compound(state, name="Silychristin A")
        assert result["identifiers"]["cas"] == "33889-69-9"
        assert result["identifiers"]["pubchem_cid"] == "443515"

    def test_extra_hints_are_applied(self, offline_lookup):
        state = CrateState()
        resolve_compound(
            state, name="Silychristin A", hints={"description": "a flavonolignan"}
        )
        mol = _by_type(state, "MolecularEntity")[0]
        assert mol.fields.get("description") == "a flavonolignan"


class TestResolveCompoundIdempotent:
    def test_second_call_no_duplicate(self, offline_lookup):
        state = CrateState()
        first = resolve_compound(state, name="Silychristin A")
        second = resolve_compound(state, name="Silychristin A")
        assert len(_by_type(state, "MolecularEntity")) == 1
        assert first["entity_id"] == second["entity_id"]


class TestResolveCompoundLookupMiss:
    def test_lookup_miss_creates_no_entity(self, monkeypatch):
        def fake_lookup(name):  # noqa: ANN001
            return {"found": False, "data": {}, "error": "not found"}

        monkeypatch.setattr(composites, "lookup_compound", fake_lookup)
        state = CrateState()
        result = resolve_compound(state, name="Nonexistadiol")

        assert result["ok"] is False
        assert "error" in result
        assert _by_type(state, "MolecularEntity") == []


class TestResolveCompoundVerificationFailure:
    def test_failed_verification_is_reported_not_fabricated(self, monkeypatch):
        """A verifier that rejects the value must NOT leave a fabricated id (D5).

        The verifier mimics the real ``verify_identifier``, which clears a value
        that does not resolve at its source. ``resolve_compound`` must surface
        the failure (verified=False) and not present the cleared id as resolved.
        """

        def fake_lookup(name):  # noqa: ANN001
            return dict(_PUBCHEM_HIT)

        def fake_verify(state, entity_id, field):  # noqa: ANN001
            # Real verify_identifier clears an unresolvable value from the entity.
            entity = state.get_entity(entity_id)
            if entity is not None:
                entity.fields.pop(field, None)
                entity.set_field_status(field, "missing", "lookup")
            return {
                "verified": False,
                "entity_id": entity_id,
                "field": field,
                "message": f"{field} could not be verified; value cleared.",
                "suggested_fix": "Provide a resolvable identifier.",
            }

        monkeypatch.setattr(composites, "lookup_compound", fake_lookup)
        monkeypatch.setattr(composites, "verify_identifier", fake_verify)

        state = CrateState()
        result = resolve_compound(state, name="Silychristin A")

        assert result["verified"] is False
        # The failure is surfaced, not swallowed.
        assert any(v["verified"] is False for v in result["verifications"])
        # D5: no unverified identifier remains on the entity masquerading as real.
        mol = _by_type(state, "MolecularEntity")[0]
        assert "cas" not in mol.fields
        assert "pubchem_cid" not in mol.fields


class TestResolveCompoundViaEngine:
    def test_callable_through_run_tool(self, monkeypatch):
        def fake_lookup(name):  # noqa: ANN001
            return dict(_PUBCHEM_HIT)

        def fake_verify(state, entity_id, field):  # noqa: ANN001
            entity = state.get_entity(entity_id)
            if entity is not None and entity.fields.get(field):
                entity.set_field_status(field, "verified", "lookup")
            return {
                "verified": True,
                "entity_id": entity_id,
                "field": field,
                "message": "ok",
                "suggested_fix": None,
            }

        monkeypatch.setattr(composites, "lookup_compound", fake_lookup)
        monkeypatch.setattr(composites, "verify_identifier", fake_verify)

        engine = AgentEngine()
        engine.initialize()
        result = engine.run_tool("resolve_compound", name="Silychristin A")
        assert result["entity_id"]
        assert engine.state.get_entity(result["entity_id"]) is not None
