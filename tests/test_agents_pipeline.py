"""Tests for the deterministic pipeline spine (Issue #179, task 4).

``run_pipeline`` is a pure, code-driven orchestrator over an already-initialized
:class:`~builder.engine.AgentEngine`: it scaffolds the ISA backbone, drafts what
state already has, builds + validates in memory, and runs a bounded deterministic
fix loop — with NO LLM deciding control flow. These tests are fully offline (the
validator runs against the bundled RO-Crate context; see
``tests/test_offline_validation.py``).

The headline guarantees under test:

* the spine on an *empty* state reaches ``{base, isa, tox}`` REQUIRED conformance;
* the bounded fix loop clears a seeded REQUIRED issue;
* the pipeline is **deterministic** — same input ⇒ identical built ``@graph`` hash
  across independent runs.
"""

from __future__ import annotations

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.hitl import SimulatedHumanInterface

# The spine drives build_and_validate / fix_required_issues, which run the
# (deliberately uncached, owlrl-heavy) SHACL validator several times — slower than
# the suite-wide CI default. Mirror tests/test_tools_repair.py and give this
# validation-heavy module headroom; the marker overrides the CLI --timeout.
pytestmark = pytest.mark.timeout(120)


def _entity(entity_id: str, type_: EntityType, **fields) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=dict(fields),
        _provenance=EntityProvenance(created_by="llm"),
    )


def _engine(state: CrateState | None = None) -> AgentEngine:
    """A headless engine on *state* (no scan), session_id assigned via initialize()."""
    engine = AgentEngine(state=state or CrateState(), human_interface=SimulatedHumanInterface())
    engine.initialize()  # assigns session_id + opens profiler; no input_path => no scan
    return engine


class TestRunPipelineShape:
    def test_returns_result_dict_with_conformance(self) -> None:
        from builder.agents.pipeline import run_pipeline

        result = run_pipeline(_engine())
        assert isinstance(result, dict)
        # The result reports the final per-layer conformance map.
        assert "conformance" in result
        assert set(result["conformance"]) == {"base", "isa", "tox"}
        assert "ok" in result


class TestEmptyStateReachesConformance:
    def test_scaffold_only_reaches_base_isa_tox(self) -> None:
        """An empty state, run through the spine, becomes {base,isa,tox}-conformant."""
        from builder.agents.pipeline import run_pipeline

        engine = _engine()
        result = run_pipeline(engine)

        conformance = result["conformance"]
        assert conformance == {"base": True, "isa": True, "tox": True}
        assert result["ok"] is True

        # The backbone really exists in state (scaffold step ran via the engine).
        types = {e.type for e in engine.state.list_entities()}
        assert {"Investigation", "Study", "Assay"} <= types

    def test_scaffold_step_supplies_required_study_name(self) -> None:
        """Regression: a bare draft_study has no `name` and fails ISA; the spine
        must supply backbone names deterministically so ISA passes with no LLM."""
        from builder.agents.pipeline import run_pipeline

        engine = _engine()
        run_pipeline(engine)
        study = next(e for e in engine.state.list_entities() if e.type == "Study")
        assert study.fields.get("name")  # non-empty


class TestBoundedFixLoop:
    def _backbone(self) -> CrateState:
        state = CrateState()
        state.metadata.title = "Pipeline fix-loop crate"
        state.add_entity(
            _entity("inv1", "Investigation", name="Inv", description="d", identifier="INV-1")
        )
        state.add_entity(
            _entity("st1", "Study", name="St", description="d", investigation_id="inv1")
        )
        state.add_entity(_entity("as1", "Assay", name="As", description="d", study_id="st1"))
        return state

    def test_fix_loop_clears_seeded_required_issue(self) -> None:
        """An EndpointReadout missing its result + exactly one un-wired File is a
        REQUIRED tox issue the deterministic fix loop must clear (link)."""
        from builder.agents.pipeline import run_pipeline

        state = self._backbone()
        state.add_entity(
            _entity(
                "er1", "LabProcess", process_type="EndpointReadout", name="Readout", assay_id="as1"
            )
        )
        state.add_entity(_entity("f0", "File", name="raw0.csv", dest_path="data/raw0.csv"))

        engine = _engine(state)
        result = run_pipeline(engine)

        # The seeded issue cleared: full conformance and the File is now wired.
        assert result["conformance"] == {"base": True, "isa": True, "tox": True}
        assert result["ok"] is True
        readout = engine.state.get_entity("er1")
        assert readout is not None
        wired = str(readout.fields.get("result") or readout.fields.get("output") or "")
        assert "f0" in wired

    def test_fix_loop_is_bounded(self) -> None:
        """The fix loop reports a bounded round count and never spins forever."""
        from builder.agents.pipeline import run_pipeline

        result = run_pipeline(_engine())
        assert isinstance(result.get("fix_rounds"), int)
        assert 0 <= result["fix_rounds"] <= 3


class TestDeterminism:
    def test_identical_graph_hash_across_runs(self) -> None:
        """Same input ⇒ identical built @graph hash — the headline win to assert."""
        from builder.agents.pipeline import run_pipeline
        from eval.metrics import crate_graph_hash

        e1 = _engine()
        run_pipeline(e1)
        h1 = crate_graph_hash(e1.state)

        e2 = _engine()
        run_pipeline(e2)
        h2 = crate_graph_hash(e2.state)

        assert h1 == h2

    def test_determinism_holds_with_seeded_entities(self) -> None:
        """Determinism holds even when state carries non-backbone entities."""
        from builder.agents.pipeline import run_pipeline
        from eval.metrics import crate_graph_hash

        def seeded() -> CrateState:
            state = CrateState()
            state.metadata.title = "Deterministic crate"
            state.add_entity(_entity("chem1", "MolecularEntity", name="Triiodothyronine"))
            return state

        e1 = _engine(seeded())
        run_pipeline(e1)
        e2 = _engine(seeded())
        run_pipeline(e2)
        assert crate_graph_hash(e1.state) == crate_graph_hash(e2.state)
