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


class TestDraftEntitiesWiring:
    """Step 2 (`_draft_entities`) wires the bounded drafter-leaf into the spine.

    The leaf (`draft_entity_fields`) is STUBBED here — no model, no network. The
    contract under test:

    * with a provider configured AND usable context, the leaf's NON-IDENTIFIER
      fields are applied to the relevant state entities;
    * D5 — identifier / `@id` / `entity_id` fields are NEVER set or overwritten;
    * missing fields are filled, existing fields are NOT overwritten;
    * with NO provider configured, the step is a STRICT no-op (nothing mutated);
    * with a provider but NO usable context, the step is also a strict no-op.
    """

    def _seeded_state(self) -> CrateState:
        """A titled crate with a backbone + a bare seeded compound to enrich."""
        state = CrateState()
        state.metadata.title = "TPO inhibition dose-response screen"
        state.metadata.description = "A cell-based in vitro TPO inhibition assay."
        state.add_entity(_entity("inv1", "Investigation", name="Inv"))
        state.add_entity(_entity("st1", "Study", name="St", investigation_id="inv1"))
        state.add_entity(_entity("as1", "Assay", name="As", study_id="st1"))
        # A bare MolecularEntity (only a name) for the leaf to enrich.
        state.add_entity(_entity("chem1", "MolecularEntity", name="Methimazole"))
        return state

    def _enable_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make `get_provider()` report a configured provider (no real model)."""
        import builder.agents.pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: "openai")

    def test_stub_leaf_applies_non_identifier_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.pipeline as pipeline_mod

        self._enable_provider(monkeypatch)
        calls: list[tuple[str, str]] = []

        def fake_leaf(entity_type, context, *, model=None):
            calls.append((entity_type, context))
            # A descriptive field plus an identifier the leaf would normally strip
            # — assert the wiring NEVER applies the identifier even if present.
            return {"description": f"drafted {entity_type}", "identifier": "FAKE-ID"}

        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", fake_leaf)

        engine = _engine(self._seeded_state())
        result = pipeline_mod._draft_entities(engine)

        # The leaf saw real context (the title appears in the gathered context).
        assert calls, "the leaf must be invoked when provider + context are present"
        assert any("TPO inhibition" in ctx for _, ctx in calls)

        # The descriptive field landed on a backbone entity and the seeded compound.
        study = engine.state.get_entity("st1")
        chem = engine.state.get_entity("chem1")
        assert study is not None and chem is not None
        assert study.fields.get("description") == "drafted Study"
        assert chem.fields.get("description") == "drafted MolecularEntity"

        # D5: the identifier the leaf returned was NOT applied to any entity.
        for ent_id in ("inv1", "st1", "as1", "chem1"):
            ent = engine.state.get_entity(ent_id)
            assert ent is not None
            assert ent.fields.get("identifier") != "FAKE-ID"
            # The entity_id / @id is untouched.
            assert ent.entity_id == ent_id

        # The result is informative.
        assert isinstance(result.get("drafted"), list)
        assert "st1" in result["drafted"]
        assert isinstance(result.get("fields_applied"), int)
        assert result["fields_applied"] >= 1

    def test_does_not_overwrite_existing_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.pipeline as pipeline_mod

        self._enable_provider(monkeypatch)

        def fake_leaf(entity_type, context, *, model=None):
            return {"name": "LEAF NAME", "description": "leaf desc"}

        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", fake_leaf)

        engine = _engine(self._seeded_state())
        pipeline_mod._draft_entities(engine)

        study = engine.state.get_entity("st1")
        assert study is not None
        # `name` was already set ("St") — the leaf must NOT clobber it…
        assert study.fields.get("name") == "St"
        # …but the missing `description` is filled.
        assert study.fields.get("description") == "leaf desc"

    def test_no_provider_is_strict_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.pipeline as pipeline_mod

        # No provider configured.
        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: None)

        # The leaf must never be called when there is no provider.
        def boom(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("draft_entity_fields must not run without a provider")

        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", boom)

        engine = _engine(self._seeded_state())
        before = {e.entity_id: dict(e.fields) for e in engine.state.list_entities()}

        result = pipeline_mod._draft_entities(engine)

        after = {e.entity_id: dict(e.fields) for e in engine.state.list_entities()}
        assert before == after, "no-provider _draft_entities must mutate nothing"
        assert result.get("drafted") == []
        assert result.get("fields_applied") == 0

    def test_no_context_is_strict_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.pipeline as pipeline_mod

        self._enable_provider(monkeypatch)

        def boom(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("draft_entity_fields must not run without context")

        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", boom)

        # An untitled, undescribed, unscanned crate carries no usable context.
        state = CrateState()
        state.add_entity(_entity("inv1", "Investigation", name="Inv"))
        engine = _engine(state)
        before = {e.entity_id: dict(e.fields) for e in engine.state.list_entities()}

        result = pipeline_mod._draft_entities(engine)

        after = {e.entity_id: dict(e.fields) for e in engine.state.list_entities()}
        assert before == after, "no-context _draft_entities must mutate nothing"
        assert result.get("drafted") == []
        assert result.get("fields_applied") == 0


class TestMaterializePlan:
    """Stage B (`_materialize_plan`) turns the extracted candidate plan into real
    domain entities via the deterministic composites (Issue #179 task 2b-B).

    `extract_plan` (the whole-document leaf) is STUBBED at its pipeline import
    site — no model, no network — and the composites' own lookups
    (`lookup_compound` / `verify_identifier` / `lookup_aop`) are stubbed too, so
    these tests are fully offline. The contract under test:

    * a canned plan materializes the expected MolecularEntity / CellLineSample /
      LabProcess chain / AOP subgraph / Person / Publication entities in state;
    * with NO provider configured, the step is a STRICT no-op;
    * with a provider but NO usable context, the step is a strict no-op;
    * D5 — no identifier from the plan is ever set on an entity; identifiers come
      only from the composites' lookups/verification;
    * idempotent — running the spine twice produces no duplicates;
    * `run_pipeline` still reaches `{base, isa, tox}` conformance with a plan.
    """

    _PLAN: dict = {
        "study": {"name": "TPO inhibition study", "description": "A TPO assay."},
        "compounds": [
            {"name": "Methimazole", "role": "test"},
            {"name": "Sodium iodide", "role": "control"},
        ],
        "cell_lines": [{"name": "FRTL-5"}],
        "process_chain": [
            {"process_type": "CellCulture", "name": "Seed cells"},
            {"process_type": "Exposure", "name": "Dose"},
            {"process_type": "EndpointReadout", "name": "Read TPO"},
            {"process_type": "DataAnalysis", "name": "Fit dose-response"},
        ],
        "aops": [{"aop_id": "610"}],
        "people": [{"name": "Ada Lovelace", "affiliation_name": "Analytical Engine"}],
        "publications": [{"title": "On TPO inhibition in vitro"}],
        "files": [{"path": "data/raw.csv", "role": "raw"}],
        "notes": "Confirm the control compound role.",
    }

    def _titled_state(self) -> CrateState:
        state = CrateState()
        state.metadata.title = "TPO inhibition dose-response screen"
        state.metadata.description = "A cell-based in vitro TPO inhibition assay."
        return state

    def _enable_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: "openai")

    def _stub_extract_plan(
        self, monkeypatch: pytest.MonkeyPatch, plan: dict | None = None
    ) -> list[str]:
        """Patch the pipeline's `extract_plan` shim to return a canned plan."""
        import builder.agents.pipeline as pipeline_mod

        seen: list[str] = []

        def fake_extract_plan(context, *, model=None):
            seen.append(context)
            return dict(self._PLAN if plan is None else plan)

        monkeypatch.setattr(pipeline_mod, "extract_plan", fake_extract_plan)
        return seen

    def _stub_lookups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stub every network lookup the composites would otherwise hit."""
        import builder.tools.composites as composites_mod
        from builder.tools import lookups as tool_lookups

        # resolve_compound -> lookup_compound (imported into composites' namespace).
        def fake_lookup_compound(name):
            return {
                "found": True,
                "data": {
                    "cas": "60-56-0",
                    "pubchem_cid": "1349907",
                    "smiles": "C1=CN(C(=S)N1)C",
                    "source": "pubchem",
                },
                "error": None,
            }

        # verify_identifier marks the field verified without touching the network.
        def fake_verify_identifier(state, entity_id, field):
            ent = state.get_entity(entity_id)
            if ent is not None:
                ent.set_field_status(field, "verified", "lookup")
            return {"verified": True, "entity_id": entity_id, "field": field, "message": "ok"}

        # materialize_aop_subgraph -> lookup_aop (imported lazily from tool_lookups).
        def fake_lookup_aop(aop_id):
            iri = f"https://aopwiki.org/aops/{aop_id}"
            mie = "https://aopwiki.org/events/1"
            ao = "https://aopwiki.org/events/2"
            ker = "https://aopwiki.org/relationships/1"
            return {
                "found": True,
                "data": {
                    "aop": {
                        "@id": iri,
                        "@type": "AdverseOutcomePathway",
                        "name": f"AOP {aop_id}",
                        "identifier": str(aop_id),
                        "url": iri,
                        "has_molecular_initiating_event": [{"@id": mie}],
                        "has_adverse_outcome": [{"@id": ao}],
                        "has_key_event_relationship": [{"@id": ker}],
                    },
                    "events": [
                        {"@id": mie, "@type": "KeyEvent", "name": "MIE",
                         "eventType": "Molecular Initiating Event"},
                        {"@id": ao, "@type": "KeyEvent", "name": "AO",
                         "eventType": "Adverse Outcome"},
                    ],
                    "relationships": [
                        {"@id": ker, "@type": "KeyEventRelationship",
                         "upstream_event": {"@id": mie},
                         "downstream_event": {"@id": ao}},
                    ],
                },
                "error": None,
            }

        monkeypatch.setattr(composites_mod, "lookup_compound", fake_lookup_compound)
        monkeypatch.setattr(composites_mod, "verify_identifier", fake_verify_identifier)
        monkeypatch.setattr(tool_lookups, "lookup_aop", fake_lookup_aop)

    def _by_type(self, engine: AgentEngine, type_name: str) -> list[Entity]:
        return [e for e in engine.state.list_entities() if e.type == type_name]

    def test_materializes_plan_entities(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.pipeline as pipeline_mod

        self._enable_provider(monkeypatch)
        seen = self._stub_extract_plan(monkeypatch)
        self._stub_lookups(monkeypatch)

        engine = _engine(self._titled_state())
        # Scaffold first so the assay/study ids exist for the chain/aop wiring.
        pipeline_mod._scaffold_backbone(engine)
        result = pipeline_mod._materialize_plan(engine)

        # The leaf saw the real gathered context (the crate title).
        assert seen and any("TPO inhibition" in ctx for ctx in seen)

        # Compounds → MolecularEntity (one per plan compound).
        chems = self._by_type(engine, "MolecularEntity")
        assert {c.fields.get("name") for c in chems} == {"Methimazole", "Sodium iodide"}

        # Cell line → CellLineSample.
        cells = self._by_type(engine, "CellLineSample")
        assert [c.fields.get("name") for c in cells] == ["FRTL-5"]

        # Process chain → 4 LabProcess steps wired to the assay.
        procs = self._by_type(engine, "LabProcess")
        ptypes = {p.fields.get("process_type") for p in procs}
        assert ptypes == {"CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"}

        # AOP → AdverseOutcomePathway subgraph.
        assert self._by_type(engine, "AdverseOutcomePathway")
        assert self._by_type(engine, "KeyEvent")

        # Person from the name, with a deterministic given/family split so it is
        # ISA-conformant (a non-empty given name is REQUIRED). No ORCID (D5).
        people = self._by_type(engine, "Person")
        ada = next(p for p in people if p.fields.get("name") == "Ada Lovelace")
        assert ada.fields.get("givenName") == "Ada"
        assert ada.fields.get("familyName") == "Lovelace"
        assert not ada.fields.get("orcid")

        # Publications are DEFERRED (title-only cannot be ISA-conformant without a
        # fabricated DOI — D5), so no Publication entity is minted; the title is
        # surfaced for a later DOI lookup instead.
        assert self._by_type(engine, "Publication") == []
        assert "On TPO inhibition in vitro" in result["publications_deferred"]
        assert result["publications"] == 0

        # The result is informative (per-section counts).
        assert result["compounds"] >= 2
        assert result["cell_lines"] >= 1
        assert result["processes"] >= 1
        assert result["aops"] >= 1
        assert result["people"] >= 1

    def test_d5_no_fabricated_identifiers_from_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D5: identifiers come from the composites' lookups, never the plan.

        The plan carries only names. A MolecularEntity's `cas`/`pubchem_cid` must
        be the LOOKED-UP value (here the stubbed `60-56-0` / `1349907`), and a
        fabricated DOI an adversarial plan smuggles in must never land on any
        entity (the title-only publication is deferred, not materialized).
        """
        import builder.agents.pipeline as pipeline_mod

        self._enable_provider(monkeypatch)
        # A plan that adversarially tries to smuggle identifiers (should be ignored
        # by the materialization — names only are passed to the composites).
        plan = {
            "compounds": [{"name": "Methimazole", "role": "test", "cas": "FAKE-CAS"}],
            "publications": [{"title": "Some paper", "doi": "10.0/FAKE"}],
        }
        self._stub_extract_plan(monkeypatch, plan)
        self._stub_lookups(monkeypatch)

        engine = _engine(self._titled_state())
        pipeline_mod._scaffold_backbone(engine)
        result = pipeline_mod._materialize_plan(engine)

        chem = self._by_type(engine, "MolecularEntity")[0]
        # The CAS is the looked-up value, NOT the plan's fabricated one.
        assert chem.fields.get("cas") == "60-56-0"
        assert chem.fields.get("cas") != "FAKE-CAS"

        # The publication is deferred (title surfaced) and no entity carries the
        # plan's fabricated DOI anywhere in state.
        assert "Some paper" in result["publications_deferred"]
        for ent in engine.state.list_entities():
            assert ent.fields.get("identifier") != "10.0/FAKE"
            assert ent.fields.get("doi") != "10.0/FAKE"

    def test_no_provider_is_strict_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: None)

        def boom(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("extract_plan must not run without a provider")

        monkeypatch.setattr(pipeline_mod, "extract_plan", boom)

        engine = _engine(self._titled_state())
        pipeline_mod._scaffold_backbone(engine)
        before = {e.entity_id for e in engine.state.list_entities()}
        result = pipeline_mod._materialize_plan(engine)
        after = {e.entity_id for e in engine.state.list_entities()}
        assert before == after, "no-provider _materialize_plan must mint nothing"
        assert result.get("compounds") == 0
        assert result.get("aops") == 0

    def test_no_context_is_strict_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.pipeline as pipeline_mod

        self._enable_provider(monkeypatch)

        def boom(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("extract_plan must not run without context")

        monkeypatch.setattr(pipeline_mod, "extract_plan", boom)

        # Untitled / undescribed / unscanned crate carries no usable context.
        engine = _engine(CrateState())
        pipeline_mod._scaffold_backbone(engine)
        before = {e.entity_id for e in engine.state.list_entities()}
        result = pipeline_mod._materialize_plan(engine)
        after = {e.entity_id for e in engine.state.list_entities()}
        assert before == after, "no-context _materialize_plan must mint nothing"
        assert result.get("compounds") == 0

    def test_idempotent_no_duplicates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.pipeline as pipeline_mod

        self._enable_provider(monkeypatch)
        self._stub_extract_plan(monkeypatch)
        self._stub_lookups(monkeypatch)

        engine = _engine(self._titled_state())
        pipeline_mod._scaffold_backbone(engine)
        pipeline_mod._materialize_plan(engine)
        counts_1 = {
            t: len(self._by_type(engine, t))
            for t in ("MolecularEntity", "CellLineSample", "LabProcess",
                      "AdverseOutcomePathway", "KeyEvent", "Person", "Publication")
        }
        # Re-run on the SAME engine — composites are idempotent, so no dups.
        pipeline_mod._materialize_plan(engine)
        counts_2 = {
            t: len(self._by_type(engine, t))
            for t in ("MolecularEntity", "CellLineSample", "LabProcess",
                      "AdverseOutcomePathway", "KeyEvent", "Person", "Publication")
        }
        assert counts_1 == counts_2

    def test_run_pipeline_with_plan_reaches_conformance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from builder.agents.pipeline import run_pipeline

        self._enable_provider(monkeypatch)
        self._stub_extract_plan(monkeypatch)
        self._stub_lookups(monkeypatch)
        # Keep the field-enrichment leaf a no-op (it is separately tested) so this
        # test isolates materialization + the existing build/fix path.
        import builder.agents.pipeline as pipeline_mod

        monkeypatch.setattr(
            pipeline_mod, "draft_entity_fields", lambda *a, **k: {}
        )

        engine = _engine(self._titled_state())
        result = run_pipeline(engine)

        assert result["conformance"] == {"base": True, "isa": True, "tox": True}
        assert result["ok"] is True
        # The materialized plan is reflected in the result trace.
        assert "materialized" in result
        assert result["materialized"]["compounds"] >= 2


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
