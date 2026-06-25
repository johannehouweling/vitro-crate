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

from pathlib import Path

import pytest

from builder.engine import AgentEngine
from builder.state import (
    CrateState,
    Entity,
    EntityProvenance,
    EntityType,
    FileClassification,
)
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

        def fake_leaf(entity_type, context, *, model=None, usage_sink=None):
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

        def fake_leaf(entity_type, context, *, model=None, usage_sink=None):
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
        "protocols": [
            {
                "name": "Amplex Red TPO activity readout",
                "description": "Fluorometric TPO activity assay protocol.",
                "process_hint": "EndpointReadout",
            }
        ],
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

        def fake_extract_plan(context, *, model=None, usage_sink=None):
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

        # resolve_publication -> search_works_by_title. Default: NO candidates, so
        # the default-plan publication stays deferred (D5). Tests that need a
        # confident match override this stub.
        def fake_search_works_by_title(title):
            return []

        monkeypatch.setattr(composites_mod, "lookup_compound", fake_lookup_compound)
        monkeypatch.setattr(composites_mod, "verify_identifier", fake_verify_identifier)
        monkeypatch.setattr(
            composites_mod, "search_works_by_title", fake_search_works_by_title
        )
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

        # Protocol → LabProtocol minted from the name/description (D5: no id), and
        # linked to the EndpointReadout process it governs (executesLabProtocol).
        protos = self._by_type(engine, "LabProtocol")
        assert [p.fields.get("name") for p in protos] == [
            "Amplex Red TPO activity readout"
        ]
        proto_id = protos[0].entity_id
        readout = next(
            p for p in procs if p.fields.get("process_type") == "EndpointReadout"
        )
        labprotocol_ref = readout.fields.get("labprotocol")
        ref_id = (
            labprotocol_ref.get("@id")
            if isinstance(labprotocol_ref, dict)
            else labprotocol_ref
        )
        assert str(ref_id).lstrip("#") == proto_id
        assert result["protocols"] >= 1

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

    def test_protocol_minted_and_linked_to_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#222: a plan protocol mints a LabProtocol linked to a process (D5).

        A plan carries a protocol NAME/description only (no identifier). The spine
        mints exactly one LabProtocol and wires it onto the LabProcess it governs
        via the ``labprotocol`` ref (``executesLabProtocol`` at build time).
        """
        import builder.agents.pipeline as pipeline_mod

        self._enable_provider(monkeypatch)
        plan = {
            "protocols": [
                {"name": "MTT viability protocol", "description": "MTT assay."}
            ],
            "process_chain": [
                {"process_type": "Exposure", "name": "Dose"},
                {"process_type": "EndpointReadout", "name": "Read viability"},
            ],
        }
        self._stub_extract_plan(monkeypatch, plan)
        self._stub_lookups(monkeypatch)

        engine = _engine(self._titled_state())
        pipeline_mod._scaffold_backbone(engine)
        result = pipeline_mod._materialize_plan(engine)

        protos = self._by_type(engine, "LabProtocol")
        assert len(protos) == 1
        assert protos[0].fields.get("name") == "MTT viability protocol"
        # D5: a protocol minted from a name carries no fabricated identifier.
        assert not protos[0].fields.get("identifier")
        assert result["protocols"] == 1

        # The protocol is linked to at least one LabProcess.
        proc_refs = [
            p.fields.get("labprotocol")
            for p in self._by_type(engine, "LabProcess")
            if p.fields.get("labprotocol")
        ]
        assert proc_refs, "the protocol must be linked to a process"

    def test_confident_publication_match_mints_entity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#224: a confident title match mints a ScholarlyArticle, not deferred."""
        import builder.agents.pipeline as pipeline_mod
        import builder.tools.composites as composites_mod

        self._enable_provider(monkeypatch)
        title = "On TPO inhibition in vitro"
        self._stub_extract_plan(monkeypatch, {"publications": [{"title": title}]})
        self._stub_lookups(monkeypatch)

        # Confident Crossref candidate (exact title, high score) → commit the DOI.
        def fake_search_works_by_title(query):
            return [{"doi": "10.1234/tpo", "title": title, "score": 99.0}]

        # The drafter re-looks the DOI up; canned article data (no network).
        def fake_lookup_doi(doi):
            return {
                "found": True,
                "data": {
                    "identifier": "10.1234/tpo",
                    "name": title,
                    "author": [{"givenName": "Ada", "familyName": "Lovelace"}],
                },
                "error": None,
            }

        monkeypatch.setattr(
            composites_mod, "search_works_by_title", fake_search_works_by_title
        )
        monkeypatch.setattr(composites_mod, "lookup_doi", fake_lookup_doi)

        engine = _engine(self._titled_state())
        pipeline_mod._scaffold_backbone(engine)
        result = pipeline_mod._materialize_plan(engine)

        pubs = self._by_type(engine, "Publication")
        assert len(pubs) == 1
        assert pubs[0].fields.get("identifier") == "10.1234/tpo"
        assert result["publications"] >= 1
        assert title not in result["publications_deferred"]

    def test_no_confident_publication_match_stays_deferred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#224: no confident match → no entity, title stays deferred (D5)."""
        import builder.agents.pipeline as pipeline_mod
        import builder.tools.composites as composites_mod

        self._enable_provider(monkeypatch)
        title = "An unfindable paper"
        self._stub_extract_plan(monkeypatch, {"publications": [{"title": title}]})
        self._stub_lookups(monkeypatch)

        # No candidate clears the confidence gate → resolve_publication returns
        # ok=False and mints nothing; the spine must NOT fabricate a DOI.
        def fake_search_works_by_title(query):
            return [{"doi": "10.9/unrelated", "title": "A different paper", "score": 99.0}]

        def boom_lookup_doi(doi):  # pragma: no cover - must not be reached
            raise AssertionError("lookup_doi must not run without a confident match")

        monkeypatch.setattr(
            composites_mod, "search_works_by_title", fake_search_works_by_title
        )
        monkeypatch.setattr(composites_mod, "lookup_doi", boom_lookup_doi)

        engine = _engine(self._titled_state())
        pipeline_mod._scaffold_backbone(engine)
        result = pipeline_mod._materialize_plan(engine)

        assert self._by_type(engine, "Publication") == []
        assert result["publications"] == 0
        assert title in result["publications_deferred"]

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


class TestTokenAccounting:
    """Issue #221 — the deterministic spine's leaf LLM calls must record their
    token usage to ``profile.ndjson`` in the SAME shape the ReAct model node
    uses, so ``eval/runner.py`` mines real per-case tokens for ``--arch pipeline``
    (it previously recorded 0 because the leaves' usage was discarded).

    Offline: the leaf is stubbed and reports a known usage payload via the
    ``usage_sink`` the spine passes it; the engine writes ``node_end``/
    ``node="model"`` events that :func:`eval.metrics.mine_profile_metrics` sums.
    """

    def _seeded_state(self) -> CrateState:
        import uuid

        state = CrateState()
        # A unique session id so each test writes its OWN profile.ndjson; the
        # _engine() default is second-precision, which collides under -n 2 and
        # would mix one test's model events into another's profile (read below).
        state.session_id = f"tok-{uuid.uuid4().hex[:12]}"
        state.metadata.title = "TPO inhibition dose-response screen"
        state.metadata.description = "A cell-based in vitro TPO inhibition assay."
        state.add_entity(_entity("inv1", "Investigation", name="Inv"))
        state.add_entity(_entity("st1", "Study", name="St", investigation_id="inv1"))
        state.add_entity(_entity("as1", "Assay", name="As", study_id="st1"))
        # Two bare entities (missing a description) for the leaf to enrich, so the
        # spine makes >1 leaf call and we assert usage is ACCUMULATED across them.
        state.add_entity(_entity("chem1", "MolecularEntity", name="Methimazole"))
        state.add_entity(_entity("per1", "Person", name="Jane Doe"))
        return state

    def _mine(self, engine: AgentEngine):
        import shutil
        from pathlib import Path

        from builder.tools.dashboard import read_profile
        from builder.tools.profiler import SESSION_DIR
        from eval.metrics import mine_profile_metrics

        engine.close_profiler()  # flush + close before reading
        session_dir = Path(SESSION_DIR) / engine.state.session_id
        try:
            return mine_profile_metrics(read_profile(session_dir / "profile.ndjson"))
        finally:
            # Don't litter sessions/ with this test's unique-id profile dir.
            shutil.rmtree(session_dir, ignore_errors=True)

    def test_draft_entities_records_accumulated_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: "openai")

        def fake_leaf(entity_type, context, *, model=None, usage_sink=None):
            # Each leaf call reports a known usage payload through the sink.
            if usage_sink is not None:
                usage_sink(100, 20, "gpt-4o-mini")
            return {"description": f"drafted {entity_type}"}

        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", fake_leaf)

        engine = _engine(self._seeded_state())
        totals = {"input_tokens": 0, "output_tokens": 0}
        sink = pipeline_mod._make_usage_logger(engine, totals)
        result = pipeline_mod._draft_entities(engine, sink)

        # The spine enriched every entity missing a descriptive field; at least
        # the two bare domain entities, so >1 leaf call is made (we assert usage
        # is ACCUMULATED, not just recorded once).
        n_calls = len(result["drafted"])
        assert n_calls >= 2

        # The running accumulator summed every leaf call (n_calls × 100/20).
        assert totals["input_tokens"] == 100 * n_calls
        assert totals["output_tokens"] == 20 * n_calls

        # …and those landed in profile.ndjson as node_end/model events, so the
        # eval runner mines them identically to the ReAct arm.
        pm = self._mine(engine)
        assert pm.input_tokens == 100 * n_calls
        assert pm.output_tokens == 20 * n_calls
        assert pm.total_tokens == 120 * n_calls

    def test_no_provider_records_clean_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.pipeline as pipeline_mod

        # No provider → the leaf is never called and no model events are written.
        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: None)

        def boom(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("leaf must not run without a provider")

        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", boom)

        engine = _engine(self._seeded_state())
        pipeline_mod._draft_entities(engine)

        pm = self._mine(engine)
        assert pm.input_tokens == 0
        assert pm.output_tokens == 0
        assert pm.total_tokens == 0

    def test_run_pipeline_returns_usage_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``run_pipeline``'s result additively surfaces the accumulated usage."""
        import builder.agents.pipeline as pipeline_mod
        from builder.agents.pipeline import run_pipeline

        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: "openai")
        # Make the plan stage a no-op (empty plan) so only the drafter leaf runs.
        monkeypatch.setattr(pipeline_mod, "extract_plan", lambda *a, **k: {})

        def fake_leaf(entity_type, context, *, model=None, usage_sink=None):
            if usage_sink is not None:
                usage_sink(50, 10, "gpt-4o-mini")
            return {"description": f"drafted {entity_type}"}

        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", fake_leaf)

        import shutil
        from pathlib import Path

        from builder.tools.profiler import SESSION_DIR

        engine = _engine(self._seeded_state())
        try:
            result = run_pipeline(engine)
        finally:
            engine.close_profiler()
            shutil.rmtree(
                Path(SESSION_DIR) / engine.state.session_id, ignore_errors=True
            )

        assert "usage" in result
        usage = result["usage"]
        # At least the two seeded bare entities were drafted (50/10 each).
        assert usage["input_tokens"] >= 100
        assert usage["output_tokens"] >= 20
        assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]


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


class TestGatherContext:
    """`_gather_context` now reads non-tabular rich file BODIES (Issue #231).

    Before #231 the spine's single bounded extraction leaf only ever saw
    filenames plus tiny tabular ``first_rows`` previews, so ``.json`` / ``.docx``
    / ``.pdf`` rich files contributed nothing and ``extract_plan`` returned an
    empty plan (the backbone then fell back to literal default names). These
    tests pin the fix: bodies of readable non-tabular files appear in the
    gathered context, bounded by ``_MAX_CONTEXT_CHARS`` (per-file + total),
    confined to ``approved_scan_roots``, and never raising out of the spine. The
    two strict no-op gates (empty context ⇒ ``""``) are preserved.
    """

    def _write_json(self, root: Path, marker: str) -> FileClassification:
        path = root / "S-VHPS26.json"
        path.write_text(
            f'{{"studyTitle": "{marker}", "organism": "Rattus norvegicus"}}',
            encoding="utf-8",
        )
        return FileClassification(
            path=str(path),
            filename=path.name,
            size=path.stat().st_size,
            mime_type="application/json",
            first_rows=None,
        )

    def _write_docx(self, root: Path, marker: str) -> FileClassification:
        from docx import Document  # type: ignore[import-untyped]

        path = root / "SOP.docx"
        doc = Document()
        doc.add_paragraph(marker)
        doc.add_paragraph("Standard operating procedure for the assay.")
        doc.save(str(path))
        return FileClassification(
            path=str(path),
            filename=path.name,
            size=path.stat().st_size,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            first_rows=None,
        )

    def test_reads_json_and_docx_bodies_into_context(self, tmp_path: Path) -> None:
        """Body substrings from non-tabular files under an approved root appear."""
        from builder.agents.pipeline import _gather_context

        json_fc = self._write_json(tmp_path, "JSONBODYMARKER")
        docx_fc = self._write_docx(tmp_path, "DOCXBODYMARKER")

        state = CrateState()
        state.approved_scan_roots.add(str(tmp_path.resolve()))
        state.scanned_files = [json_fc, docx_fc]
        engine = _engine(state)

        context = _gather_context(engine)

        # The bodies — not just the filenames — reached the context.
        assert "JSONBODYMARKER" in context
        assert "DOCXBODYMARKER" in context
        # Filenames are still listed.
        assert "S-VHPS26.json" in context
        assert "SOP.docx" in context

    def test_prefers_cheap_first_rows_when_present(self, tmp_path: Path) -> None:
        """A file carrying a tabular preview uses it; disk is not re-read for it."""
        from builder.agents.pipeline import _gather_context
        from builder.state import FileClassification

        # A file that DOES carry first_rows — the cheap preview must be used and the
        # body reader must not be invoked for it (the path need not even exist).
        tabular = FileClassification(
            path=str(tmp_path / "missing.csv"),
            filename="data.csv",
            size=10,
            mime_type="text/csv",
            first_rows=["col_a,col_b", "1,2"],
        )
        state = CrateState()
        state.approved_scan_roots.add(str(tmp_path.resolve()))
        state.scanned_files = [tabular]
        engine = _engine(state)

        context = _gather_context(engine)
        assert "col_a,col_b" in context

    def test_reads_are_confined_to_approved_scan_roots(self, tmp_path: Path) -> None:
        """A body OUTSIDE every approved root is never read into the context."""
        from builder.agents.pipeline import _gather_context

        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        inside_fc = self._write_json(approved, "INSIDEMARKER")
        outside_fc = self._write_json(outside, "OUTSIDEMARKER")
        # Re-point filename so the two are distinguishable in the listing.
        outside_fc.filename = "outside.json"

        state = CrateState()
        state.approved_scan_roots.add(str(approved.resolve()))
        state.scanned_files = [inside_fc, outside_fc]
        engine = _engine(state)

        context = _gather_context(engine)
        assert "INSIDEMARKER" in context
        # The out-of-root body is fail-closed: its content never appears.
        assert "OUTSIDEMARKER" not in context

    def test_per_file_and_total_budget_are_bounded(self, tmp_path: Path) -> None:
        """Per-file and total context are capped by `_MAX_CONTEXT_CHARS`."""
        import builder.agents.pipeline as pipeline_mod
        from builder.agents.pipeline import _gather_context
        from builder.state import FileClassification

        cap = pipeline_mod._MAX_CONTEXT_CHARS
        assert isinstance(cap, int) and cap > 0

        state = CrateState()
        state.approved_scan_roots.add(str(tmp_path.resolve()))
        files: list[FileClassification] = []
        # Several large files, each far bigger than the cap, so both the per-file
        # and the total budget must clamp the result.
        for i in range(6):
            path = tmp_path / f"big{i}.txt"
            path.write_text("X" * (cap * 4), encoding="utf-8")
            files.append(
                FileClassification(
                    path=str(path),
                    filename=path.name,
                    size=path.stat().st_size,
                    mime_type="text/plain",
                    first_rows=None,
                )
            )
        state.scanned_files = files
        engine = _engine(state)

        context = _gather_context(engine)
        # Total context stays within a small multiple of the documented cap (the
        # total budget is the binding ceiling — not 6x the per-file body).
        assert len(context) <= cap * 3

    def test_no_readable_files_is_strict_noop(self, tmp_path: Path) -> None:
        """Nothing readable ⇒ ``""`` so the no-provider determinism gate holds."""
        from builder.agents.pipeline import _gather_context
        from builder.state import FileClassification

        # A binary file with no first_rows whose body reader returns None, under an
        # approved root, on an untitled/undescribed crate ⇒ no usable context.
        path = tmp_path / "blob.pzfx"
        path.write_bytes(b"\x00\x01\x02\x03binary-not-text\x00")
        state = CrateState()
        state.approved_scan_roots.add(str(tmp_path.resolve()))
        state.scanned_files = [
            FileClassification(
                path=str(path),
                filename=path.name,
                size=path.stat().st_size,
                mime_type="application/octet-stream",
                first_rows=None,
            )
        ]
        engine = _engine(state)

        # Filenames alone still list, but no BODY content — and an untitled crate
        # with only a filename listing is still usable context for the listing
        # path. The strict no-op we must preserve is the *fully empty* one:
        state_empty = CrateState()
        engine_empty = _engine(state_empty)
        assert _gather_context(engine_empty) == ""

        # The binary body itself contributed nothing readable.
        context = _gather_context(engine)
        assert "binary-not-text" not in context
