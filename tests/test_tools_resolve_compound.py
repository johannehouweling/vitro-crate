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

        Note (#261): the lookup here returns NO ``pubchem_cid``, so there is no
        authoritative CID to trust — both ``cas`` and any drafted CID go through
        the (rejecting) ``verify_identifier`` and are cleared. This keeps the test
        squarely on the "source rejects the value" D5 path that the CID
        short-circuit deliberately does not cover.
        """
        unconfirmable = {
            "found": True,
            "error": None,
            # No pubchem_cid in the authoritative data -> nothing to short-circuit.
            "data": {"cas": "33889-69-9", "iupac_name": "silychristin A"},
        }

        def fake_lookup(name):  # noqa: ANN001
            return dict(unconfirmable)

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
        # A hint-supplied CID is NOT the authority's answer (the lookup has none),
        # so it must be re-verified -> rejected -> cleared (D5).
        result = resolve_compound(
            state, name="Silychristin A", hints={"pubchem_cid": "443515"}
        )

        assert result["verified"] is False
        # The failure is surfaced, not swallowed.
        assert any(v["verified"] is False for v in result["verifications"])
        # D5: no unverified identifier remains on the entity masquerading as real.
        mol = _by_type(state, "MolecularEntity")[0]
        assert "cas" not in mol.fields
        assert "pubchem_cid" not in mol.fields


class TestResolveCompoundCidVerification:
    """The PubChem CID returned by the primary name lookup must be KEPT (#261).

    These tests exercise the REAL ``verify_identifier`` (not a mock) against a
    PubChem ``/compound/name`` endpoint mock that behaves like the real one: it
    resolves a compound *name* and a CAS RN (both are findable on the name
    endpoint) but NOT a bare numeric CID — a CID is not a *name*, so the name
    endpoint returns nothing for it. On current main the ``pubchem_cid`` verify
    re-resolves the CID as the query ``"CID <cid>"`` against this name endpoint,
    finds nothing, and D5 clears the very CID PubChem just returned for the name
    — losing a primary identifier on every compound.
    """

    @pytest.fixture
    def realistic_pubchem(self, monkeypatch):
        """Patch the underlying PubChem client + neutralise the warm cache.

        Mimics PubChem ``/compound/name/<q>/JSON``: a known *name* or *CAS RN*
        resolves to the Quercetin record; anything else (notably a bare CID)
        misses. Crucially the in-process resolve cache is left COLD for the verify
        step (``warm_compound_cache`` is stubbed to a no-op) so the CID
        verification cannot be served a warmed record — this reproduces the real
        run, where the warm cache (#252) does not mask the broken reverse lookup
        and the CID is wrongly cleared on every compound (#261).
        """
        from builder.tools import lookups as facade
        from builder.tools._resolve_cache import compound_cache

        record = {
            "cas": "117-39-5",
            "smiles": "",
            "inchikey": "REFJWTPEDVJJIY-UHFFFAOYSA-N",
            "inchi": "",
            "formula": "C15H10O7",
            "mass": "302.23",
            "iupac_name": "quercetin",
            "pubchem_cid": "5280343",
        }
        names_seen: list[str] = []

        def fake_pubchem(name):  # noqa: ANN001
            names_seen.append(name)
            key = name.strip().lower()
            if key in {"quercetin", "117-39-5"}:
                return dict(record)
            return {}  # a bare CID is NOT a PubChem name -> miss

        monkeypatch.setattr(facade, "lookup_pubchem", fake_pubchem)
        monkeypatch.setattr(facade, "lookup_ontology_term_ols", lambda raw, ont: {})
        # Defeat the warm cache so the CID verify hits the (broken) cold path —
        # the exact condition the real run exhibits (the warm cache otherwise
        # masks the bug). Patched on the composites namespace where it is called.
        monkeypatch.setattr(composites, "warm_compound_cache", lambda *a, **k: None)
        facade.lookup_compound.cache_clear()
        compound_cache.clear()
        yield names_seen
        facade.lookup_compound.cache_clear()
        compound_cache.clear()

    def test_primary_cid_is_kept_and_verified(self, realistic_pubchem):
        """The CID from the primary name lookup is KEPT (verified), not cleared."""
        state = CrateState()
        result = resolve_compound(state, name="Quercetin")

        mol = _by_type(state, "MolecularEntity")[0]
        # The authoritative CID survives on the entity.
        assert mol.fields.get("pubchem_cid") == "5280343"
        status = mol.get_field_status("pubchem_cid")
        assert status is not None and status.status == "verified"

        verdicts = {v["field"]: v["verified"] for v in result["verifications"]}
        assert verdicts.get("pubchem_cid") is True
        assert result["identifiers"].get("pubchem_cid") == "5280343"

    def test_cas_verification_unchanged(self, realistic_pubchem):
        """CAS still verifies via the name endpoint (no regression)."""
        state = CrateState()
        result = resolve_compound(state, name="Quercetin")

        mol = _by_type(state, "MolecularEntity")[0]
        assert mol.fields.get("cas") == "117-39-5"
        status = mol.get_field_status("cas")
        assert status is not None and status.status == "verified"
        verdicts = {v["field"]: v["verified"] for v in result["verifications"]}
        assert verdicts.get("cas") is True
        # Both identifiers confirmed -> overall verified.
        assert result["verified"] is True

    def test_lookup_cid_wins_over_conflicting_hint_and_is_verified(
        self, realistic_pubchem
    ):
        """The authoritative CID overrides a conflicting hint and is kept+verified.

        Identifier/source fields win over caller hints, so the looked-up CID
        replaces a stale hint value; that authoritative CID is then verified and
        survives (the hint never lingers as a fabricated id).
        """
        state = CrateState()
        result = resolve_compound(
            state, name="Quercetin", hints={"pubchem_cid": "99999999"}
        )
        mol = _by_type(state, "MolecularEntity")[0]
        assert mol.fields.get("pubchem_cid") == "5280343"
        verdicts = {v["field"]: v["verified"] for v in result["verifications"]}
        assert verdicts.get("pubchem_cid") is True


class TestResolveCompoundUnconfirmableCidCleared:
    """A drafted CID that is NOT the primary-lookup answer is still cleared (D5)."""

    def test_stale_cid_on_existing_entity_is_cleared(self, monkeypatch):
        """D5 holds: a CID that does not match the authority's answer is dropped.

        The primary lookup returns a record WITHOUT a CID (e.g. a ChEBI-style
        hit), but the entity already carries a (fabricated/stale) ``pubchem_cid``.
        That value is not the authority's primary key for this name, so the verify
        must clear it rather than rubber-stamp it.
        """
        from builder.tools import lookups as facade
        from builder.tools._resolve_cache import compound_cache

        # Primary lookup: a hit with NO pubchem_cid (so nothing authoritative to
        # rubber-stamp); the real verify is then exercised for the planted CID.
        def fake_lookup(name):  # noqa: ANN001
            return {
                "found": True,
                "error": None,
                "data": {"cas": "117-39-5", "iupac_name": "quercetin"},
            }

        # The underlying client never resolves a bare CID via the name endpoint.
        def fake_pubchem(name):  # noqa: ANN001
            key = name.strip().lower()
            if key == "117-39-5":
                return {"cas": "117-39-5", "pubchem_cid": "5280343"}
            return {}

        monkeypatch.setattr(composites, "lookup_compound", fake_lookup)
        monkeypatch.setattr(facade, "lookup_pubchem", fake_pubchem)
        monkeypatch.setattr(facade, "lookup_ontology_term_ols", lambda raw, ont: {})
        facade.lookup_compound.cache_clear()
        compound_cache.clear()

        state = CrateState()
        # First resolve mints the entity (no CID from the lookup).
        resolve_compound(state, name="Quercetin")
        mol = _by_type(state, "MolecularEntity")[0]
        # Plant a stale/unconfirmable CID directly on the entity.
        mol.fields["pubchem_cid"] = "11111111"
        mol.set_field_status("pubchem_cid", "filled", "llm")

        # Re-resolve: the lookup still returns no authoritative CID, so the planted
        # CID must be verified the normal way -> unconfirmable -> cleared (D5).
        resolve_compound(state, name="Quercetin")
        assert "pubchem_cid" not in mol.fields
        status = mol.get_field_status("pubchem_cid")
        assert status is not None and status.status == "missing"

        facade.lookup_compound.cache_clear()
        compound_cache.clear()


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
