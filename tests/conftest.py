"""Shared test fixtures for the builder test suite."""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance


@pytest.fixture(autouse=True)
def _stub_composites_dtxsid(monkeypatch):
    """Keep ``resolve_compound``'s best-effort CompTox DTXSID lookup offline (#179).

    ``resolve_compound`` runs a best-effort ``lookup_dtxsid`` (a live CompTox HTTP
    call) after the primary PubChem/ChEBI resolution. Default it to a MISS across
    the whole suite so no offline test accidentally reaches the network; tests
    that exercise the DTXSID path re-patch ``composites.lookup_dtxsid`` with a hit
    (a later ``monkeypatch.setattr`` in the test wins). Scoped to the symbol bound
    in the ``composites`` namespace, so direct ``lookups`` / ``comptox`` (and
    contract) tests are untouched. ``raising=False`` tolerates any import order.
    """
    from builder.tools import composites

    monkeypatch.setattr(
        composites,
        "lookup_dtxsid",
        lambda query: {"found": False, "data": {}, "error": "offline stub (conftest)"},
        raising=False,
    )


@pytest.fixture(autouse=True)
def _stub_composites_compound(monkeypatch):
    """Keep ``resolve_compound``'s PRIMARY PubChem lookup offline too (#338).

    Sibling of :func:`_stub_composites_dtxsid`, and needed for the same reason at
    a new call site: the spine now retries every identifier-less compound
    (``_retry_unresolved_compounds``), so any test that runs ``run_pipeline`` with
    a provider configured and an unresolved MolecularEntity in state reaches
    PubChem — including tests written long before the retry existed, which stub
    only the leaves. ``tests/test_agents_pipeline.py::TestTokenAccounting`` seeds
    exactly that shape.

    A live call there is not a slow test, it is a flaky one: PubChem 429s put
    ``resolve_compound`` at 30-66s against this module's 120s cap while
    ``_resolve_cache.DEFAULT_RESOLVE_TIMEOUT`` is 240s, so the pytest timeout
    fires first and the failure carries no diagnostic. It also poisons the
    process-global ``compound_cache`` for every later test in the same xdist
    worker.

    Defaults to a MISS. Tests exercising the resolution path re-patch
    ``composites.lookup_compound`` with a hit (a later ``monkeypatch.setattr``
    wins). Scoped to the symbol bound in the ``composites`` namespace, so the
    ``lookups`` / ``pubchem`` contract tests are untouched.
    """
    from builder.tools import composites

    monkeypatch.setattr(
        composites,
        "lookup_compound",
        lambda name, **_kw: {"found": False, "data": {}, "error": "offline stub (conftest)"},
        raising=False,
    )


@pytest.fixture
def minimal_state() -> CrateState:
    """Return a CrateState with one Investigation entity."""
    state = CrateState()
    entity = Entity(
        entity_id="inv_001",
        type="Investigation",
        fields={"title": "Test Investigation", "description": "A test"},
        _provenance=EntityProvenance(created_by="llm"),
    )
    state.add_entity(entity)
    return state


@pytest.fixture
def state_with_multiple_entities() -> CrateState:
    """Return a CrateState with entities of different types for filtering tests."""
    state = CrateState()

    inv = Entity(
        entity_id="inv_001",
        type="Investigation",
        fields={"title": "My Investigation"},
        _provenance=EntityProvenance(created_by="llm"),
    )
    state.add_entity(inv)

    study = Entity(
        entity_id="stu_001",
        type="Study",
        fields={"title": "My Study"},
        _provenance=EntityProvenance(created_by="llm"),
    )
    state.add_entity(study)

    mol = Entity(
        entity_id="chem_001",
        type="MolecularEntity",
        fields={"name": "Test Compound"},
        _provenance=EntityProvenance(created_by="lookup"),
    )
    state.add_entity(mol)

    return state
