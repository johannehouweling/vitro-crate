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
def _stub_composites_cellosaurus(monkeypatch):
    """Keep ``resolve_cell_line``'s Cellosaurus calls offline (#372).

    Wiring ``resolve_cell_line`` into ``_materialize_plan`` gave three modules a
    live network path they never had: ``test_agents_pipeline``,
    ``test_pipeline_e2e`` and ``test_pipeline_real_input`` all drive the real
    pipeline over a plan carrying a cell line, and nothing stubbed Cellosaurus.
    Default both primitives to a MISS so a forgotten stub surfaces as "no
    accession" rather than as a request to api.cellosaurus.org.

    **Scoped to the symbols bound in the ``composites`` namespace**, exactly like
    ``_stub_composites_dtxsid`` above — NOT to ``builder.tools.lookups``, which
    the issue's plan proposed. Patching there is measurably wrong: nine tests in
    ``test_lookups_cellosaurus_recall`` drive the REAL
    ``builder.tools.lookups.lookup_cell_line_by_name`` with its HTTP replayed by
    ``responses``, and stubbing the search it calls short-circuits the very
    recall/D5-gate behaviour they pin. "Do not reach the network" and "do not
    resolve" are different claims, and only the first one is this fixture's job.

    Tests that need a hit re-patch these two names (a later ``monkeypatch.setattr``
    wins); ``tests/test_composites_resolve_cell_line.py`` instead restores the real
    primitives and stubs one layer down, so the D5 gate stays under test.
    ``raising=False`` tolerates any import order.
    """
    from builder.tools import composites

    monkeypatch.setattr(
        composites,
        "lookup_cell_line_by_name",
        lambda name: {"found": False, "data": {}, "error": "offline stub (conftest)"},
        raising=False,
    )
    monkeypatch.setattr(
        composites,
        "lookup_cell_line",
        lambda accession: {"found": False, "data": {}, "error": "offline stub (conftest)"},
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
