"""End-to-end pipeline regression guard for the #179 thesis (AGENTS.md §14).

This is the *whole-spine* counterpart to the unit-level
``tests/test_agents_pipeline.py``: it drives the full
:func:`builder.agents.pipeline.pipeline.run_pipeline` loop (scaffold → ``_materialize_plan``
→ enrich → build/validate → fix) with the **bounded LLM leaves stubbed** and the
composites' network lookups replaced by deterministic canned data, then locks in
the three claims the §14 ReAct→deterministic-pipeline migration rests on:

1. **Conformance.** With a realistic candidate plan for a complete in-vitro tox
   study, the deterministic spine builds a crate that passes ``{base, isa, tox}``
   REQUIRED conformance.
2. **Fidelity.** The build produces at least the
   ``arbitrary-tox-folder`` corpus ``min_entities`` floor (imported from
   :mod:`eval.corpus` so the bar can't silently drift) and carries the expected
   entity *types* — a MolecularEntity, a CellLine Sample, and the four-step
   CellCulture → Exposure → EndpointReadout → DataAnalysis LabProcess chain.
3. **Determinism (the headline claim) — NON-BLOCKING.** Run twice from fresh state
   with the SAME stubbed leaf outputs, the stable @graph hash
   (:func:`eval.metrics.crate_graph_hash`, the exact signal the A/B harness uses)
   is byte-identical — the goal being to prove the deterministic core is
   reproducible given fixed leaf outputs, leaving the bounded LLM leaves as the
   only source of nondeterminism. Stubbed-spine determinism is plausible but **not
   yet established** for #179, so this single test is ``xfail(strict=False)``: it
   surfaces the signal (XPASS when it holds, XFAIL when it doesn't) without ever
   reddening CI. It is promoted to a hard assert once the eval ``determinism_rate``
   confirms it. Conformance and fidelity (below) ARE hard asserts.

Plus a **no-op safety** check: with no LLM provider configured, ``run_pipeline``
still reaches base/isa/tox via the pure scaffold path (the leaves are strict
no-ops, so the spine never crashes and never needs a model).

Everything here is fully offline — NO network, NO live LLM. The bounded leaves
(``extract_plan`` / ``draft_entity_fields``) are patched at their pipeline import
sites, and the composites' lookups (``lookup_compound`` / ``verify_identifier`` /
``lookup_aop``) are patched to return canned data, so the test exercises the real
deterministic spine end to end without ever leaving the process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.hitl import SimulatedHumanInterface

# run_pipeline drives build_and_validate / fix_required_issues, which run the
# (deliberately uncached, owlrl-heavy) SHACL validator several times — and this
# module runs the WHOLE spine multiple times (conformance + fidelity + a 2x
# determinism check), so it is heavier still than tests/test_agents_pipeline.py.
# CI runs pytest with --timeout=30; this marker overrides it for this module.
pytestmark = pytest.mark.timeout(120)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _entity(entity_id: str, type_: EntityType, **fields: Any) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=dict(fields),
        _provenance=EntityProvenance(created_by="llm"),
    )


def _engine(state: CrateState | None = None) -> AgentEngine:
    """A headless engine on *state* (no scan); session_id assigned via initialize()."""
    engine = AgentEngine(
        state=state or CrateState(), human_interface=SimulatedHumanInterface()
    )
    engine.initialize()  # assigns session_id + opens profiler; no input_path => no scan
    return engine


def _titled_state() -> CrateState:
    """A titled/described crate so the leaves' context gate is satisfied.

    The spine's ``_gather_context`` builds its free-text context from the crate
    title + description, so a titled crate is what makes the stubbed
    ``extract_plan`` / ``draft_entity_fields`` leaves run (rather than short-circuit
    as a no-op).
    """
    state = CrateState()
    state.metadata.title = "TPO inhibition dose-response screen"
    state.metadata.description = (
        "A cell-based in vitro assay screening Methimazole for its capacity to "
        "inhibit thyroid peroxidase (TPO) activity in a TPO-overexpressing FRTL-5 "
        "rat thyroid follicular cell model, reported as a dose-response IC50."
    )
    return state


# A realistic candidate plan for a COMPLETE in-vitro tox study: a study, a
# test + a control compound, a cell line, the full four-step
# CellCulture → Exposure → EndpointReadout → DataAnalysis chain, an AOP, a
# person, a publication (deferred, title-only), and a couple of files. This is
# the shape the real ``extract_plan`` leaf returns (names only — D5: no
# identifiers; the composites resolve those from the stubbed lookups).
_PLAN: dict[str, Any] = {
    "study": {
        "name": "TPO inhibition dose-response study",
        "description": "A cell-based in vitro TPO inhibition dose-response assay.",
    },
    "compounds": [
        {"name": "Methimazole", "role": "test"},
        {"name": "Sodium iodide", "role": "control"},
    ],
    "cell_lines": [{"name": "FRTL-5 TPO-overexpressing cells"}],
    "protocols": [
        {
            "name": "Amplex Red fluorometric TPO activity readout",
            "description": "Fluorometric TPO activity assay protocol.",
            "process_hint": "EndpointReadout",
        }
    ],
    "process_chain": [
        {"process_type": "CellCulture", "name": "FRTL-5 cell culture"},
        {
            "process_type": "Exposure",
            "name": "Methimazole exposure",
            "parameters": {"duration": "30 minutes", "microplate": "96-well"},
        },
        {
            "process_type": "EndpointReadout",
            "name": "Amplex Red TPO readout",
            "parameters": {
                "detection_instrument": "Wizard2 gamma counter",
                "instrument_manufacturer": "Perkin Elmer",
                "endpoint": "TPO activity",
                "technical_replicate": "3",
            },
        },
        {"process_type": "DataAnalysis", "name": "Dose-response IC50 analysis"},
    ],
    "aops": [{"aop_id": "610"}],
    "people": [
        {"name": "Marije Vonk", "affiliation_name": "Universiteit Utrecht"}
    ],
    "publications": [{"title": "On TPO inhibition in vitro"}],
    "files": [
        {"path": "measurements/dose_response_raw.csv", "role": "raw"},
        {"path": "analysis/ic50_results.csv", "role": "processed"},
    ],
    "notes": "Confirm the control compound role.",
}


def _stub_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the bounded LLM leaves + the composites' network lookups (offline).

    Patches, at the pipeline import sites and in the composites' namespace, every
    place the spine would otherwise reach for a model or the network:

    * ``get_provider`` → reports a configured provider (so the materialize/draft
      steps are NOT short-circuited as no-ops), without any real model;
    * ``extract_plan`` (Stage A leaf) → returns the canned :data:`_PLAN`;
    * ``draft_entity_fields`` (drafter leaf) → returns a deterministic descriptive
      field so enrichment is exercised but stays byte-stable across runs;
    * ``lookup_compound`` / ``verify_identifier`` (resolve_compound's lookups) →
      canned, verified identifiers;
    * ``lookup_aop`` (materialize_aop_subgraph's lookup) → a canned AOP subgraph.

    Mirrors the stubs in ``tests/test_agents_pipeline.py`` so this e2e test and the
    unit tests stay consistent. All stubs are deterministic, so the spine's only
    source of nondeterminism (the bounded leaves) is pinned for the determinism
    assertion.
    """
    import builder.agents.pipeline.pipeline as pipeline_mod
    import builder.tools.composites as composites_mod
    from builder.tools import lookups as tool_lookups
    from builder.tools._resolve_cache import compound_cache

    # The compound resolution cache is process-global; a prior test can pre-cache
    # these names and short-circuit the lookup, masking this stub's per-name CID.
    # Clear it so the stub always runs fresh (xdist-safe).
    compound_cache.clear()

    # Provider gate: configured (no real model) so the leaves run.
    monkeypatch.setattr(pipeline_mod, "get_provider", lambda: "openai")

    # Stage A leaf — the whole-document candidate-plan extractor.
    def fake_extract_plan(
        context: str, *, model: str | None = None, usage_sink: Any = None
    ) -> dict[str, Any]:
        return dict(_PLAN)

    monkeypatch.setattr(pipeline_mod, "extract_plan", fake_extract_plan)

    # Drafter leaf — deterministic descriptive fields (no identifiers; the spine
    # strips them anyway). Constant per type so the @graph hash is stable.
    def fake_draft_entity_fields(
        entity_type: str,
        context: str,
        *,
        model: str | None = None,
        usage_sink: Any = None,
    ) -> dict[str, Any]:
        return {"description": f"Drafted {entity_type} description."}

    monkeypatch.setattr(
        pipeline_mod, "draft_entity_fields", fake_draft_entity_fields
    )

    # resolve_compound → lookup_compound (imported into composites' namespace).
    # A DISTINCT CID per compound name: the dedup-by-chemical-identity path
    # (Issue #179) collapses two names that resolve to the SAME identity into one
    # MolecularEntity, so two DISTINCT plan compounds must carry distinct CIDs to
    # stay distinct nodes. CAS stays constant (`60-56-0`) so the D5 exact-value
    # assertion is preserved; only the (node-id-bearing) CID varies per name.
    _cids = {"Methimazole": "1349907", "Sodium iodide": "5238"}

    def fake_lookup_compound(name: str) -> dict[str, Any]:
        return {
            "found": True,
            "data": {
                "cas": "60-56-0",
                "pubchem_cid": _cids.get(name, "999999"),
                "smiles": "C1=CN(C(=S)N1)C",
                "source": "pubchem",
            },
            "error": None,
        }

    # verify_identifier marks the field verified without touching the network.
    def fake_verify_identifier(
        state: CrateState, entity_id: str, field: str
    ) -> dict[str, Any]:
        ent = state.get_entity(entity_id)
        if ent is not None:
            ent.set_field_status(field, "verified", "lookup")
        return {
            "verified": True,
            "entity_id": entity_id,
            "field": field,
            "message": "ok",
        }

    # materialize_aop_subgraph → lookup_aop (imported lazily from tool_lookups).
    def fake_lookup_aop(aop_id: str) -> dict[str, Any]:
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
                    {
                        "@id": mie,
                        "@type": "KeyEvent",
                        "name": "MIE",
                        "eventType": "Molecular Initiating Event",
                    },
                    {
                        "@id": ao,
                        "@type": "KeyEvent",
                        "name": "AO",
                        "eventType": "Adverse Outcome",
                    },
                ],
                "relationships": [
                    {
                        "@id": ker,
                        "@type": "KeyEventRelationship",
                        "upstream_event": {"@id": mie},
                        "downstream_event": {"@id": ao},
                    },
                ],
            },
            "error": None,
        }

    # resolve_publication → search_works_by_title. No candidates (offline), so the
    # title-only publication stays deferred (D5) and the build is deterministic.
    def fake_search_works_by_title(title: str) -> list[dict[str, Any]]:
        return []

    # NB: resolve_compound's best-effort CompTox DTXSID lookup is stubbed offline
    # suite-wide by the ``_stub_composites_dtxsid`` autouse fixture in conftest.py.
    monkeypatch.setattr(composites_mod, "lookup_compound", fake_lookup_compound)
    monkeypatch.setattr(composites_mod, "verify_identifier", fake_verify_identifier)
    monkeypatch.setattr(
        composites_mod, "search_works_by_title", fake_search_works_by_title
    )
    monkeypatch.setattr(tool_lookups, "lookup_aop", fake_lookup_aop)


def _arbitrary_tox_min_entities() -> dict[str, int]:
    """The ``arbitrary-tox-folder`` corpus ``min_entities`` floor.

    Imported from :mod:`eval.corpus` so the fidelity bar this test enforces is the
    SAME number the A/B harness uses — it cannot silently drift away from the
    corpus floor.
    """
    from eval.corpus import DEFAULT_CORPUS

    case = next(c for c in DEFAULT_CORPUS if c.case_id == "arbitrary-tox-folder")
    assert case.min_entities is not None, "the corpus floor must be declared"
    return case.min_entities


# ---------------------------------------------------------------------------
# 1 + 2: conformance + fidelity (one full pipeline run, asserted from many angles)
# ---------------------------------------------------------------------------


class TestPipelineE2EConformanceAndFidelity:
    """A full ``run_pipeline`` over a stubbed plan builds a complete, conformant crate."""

    def test_conformance_base_isa_tox_all_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Conformance: the deterministic spine reaches {base, isa, tox} REQUIRED."""
        from builder.agents.pipeline.pipeline import run_pipeline

        _stub_leaves(monkeypatch)
        engine = _engine(_titled_state())
        result = run_pipeline(engine)

        assert result["conformance"] == {"base": True, "isa": True, "tox": True}
        assert result["ok"] is True

    def test_fidelity_meets_corpus_min_entities_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fidelity: the build's entity count meets the imported corpus floor.

        The bar is the SUM of the ``arbitrary-tox-folder`` ``min_entities`` floor
        (imported from :mod:`eval.corpus` so it can't drift): the build must
        produce at least as many entities as that complete-study floor demands. A
        build that drops the four-step derivation chain (or any other chunk of the
        study) falls below the count and trips this guard.

        NOTE: the bar asserted here is the *total* count, not the corpus' per-type
        quota. As of #222/#224 the spine DOES mint a ``LabProtocol`` from the plan's
        protocol section (asserted directly in
        :meth:`test_fidelity_expected_entity_types_present`); the summed-count bar is
        kept here as the honest, non-drifting aggregate fidelity floor. (The spine
        over-delivers on the chain, files, AOP subgraph, and now the protocol.)
        """
        from builder.agents.pipeline.pipeline import run_pipeline

        floor = _arbitrary_tox_min_entities()

        _stub_leaves(monkeypatch)
        engine = _engine(_titled_state())
        run_pipeline(engine)

        # Total entity count >= the summed floor (the bar imported from corpus).
        total = len(engine.state.list_entities())
        assert total >= sum(floor.values()), (
            f"built {total} entities; corpus floor demands >= {sum(floor.values())}"
        )

    def test_fidelity_expected_entity_types_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fidelity: the expected domain entity types are all materialized.

        MolecularEntity (test + control compound), a CellLine Sample, and the four
        LabProcess steps of the CellCulture → Exposure → EndpointReadout →
        DataAnalysis chain — the structure a *complete* in-vitro tox study needs.
        """
        from builder.agents.pipeline.pipeline import run_pipeline

        _stub_leaves(monkeypatch)
        engine = _engine(_titled_state())
        run_pipeline(engine)

        state = engine.state

        # The ISA backbone the scaffold step lays down.
        assert {"Investigation", "Study", "Assay"} <= {
            e.type for e in state.list_entities()
        }

        # MolecularEntity — both plan compounds resolved (names only; ids from lookup).
        chems = state.list_entities("MolecularEntity")
        assert {c.fields.get("name") for c in chems} == {"Methimazole", "Sodium iodide"}
        # D5: the CAS is the LOOKED-UP value, never fabricated by the plan/leaf.
        # No vacuous `if c.fields.get("cas")` guard — every resolved compound MUST
        # carry the looked-up CAS, so a regression that stops populating it fails here
        # instead of passing over an empty set.
        assert chems
        assert all(c.fields.get("cas") == "60-56-0" for c in chems)

        # A CellLine Sample.
        cells = state.list_entities("CellLineSample")
        assert [c.fields.get("name") for c in cells] == [
            "FRTL-5 TPO-overexpressing cells"
        ]

        # The full four-step LabProcess derivation chain.
        procs = state.list_entities("LabProcess")
        assert {p.fields.get("process_type") for p in procs} == {
            "CellCulture",
            "Exposure",
            "EndpointReadout",
            "DataAnalysis",
        }

        # #222: a LabProtocol is minted from the plan and linked to the process it
        # governs (executesLabProtocol) — the per-type corpus floor demands >= 1.
        protos = state.list_entities("LabProtocol")
        assert protos, "the spine must mint a LabProtocol from the plan"
        linked = [
            p.fields.get("labprotocol") for p in procs if p.fields.get("labprotocol")
        ]
        assert linked, "the LabProtocol must be linked to a LabProcess"

    def test_result_trace_reflects_materialized_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The result dict's trace mirrors what the stubbed plan materialized."""
        from builder.agents.pipeline.pipeline import run_pipeline

        _stub_leaves(monkeypatch)
        engine = _engine(_titled_state())
        result = run_pipeline(engine)

        materialized = result["materialized"]
        assert materialized["compounds"] >= 2
        assert materialized["cell_lines"] >= 1
        assert materialized["processes"] >= 1
        assert materialized["aops"] >= 1
        assert materialized["people"] >= 1
        # The fix loop is bounded.
        assert isinstance(result["fix_rounds"], int)
        assert 0 <= result["fix_rounds"] <= 3


# ---------------------------------------------------------------------------
# 3: determinism — the headline claim
# ---------------------------------------------------------------------------


class TestPipelineE2EDeterminism:
    """Same stubbed leaf outputs ⇒ byte-identical built @graph across fresh runs.

    NON-BLOCKING. Stubbed-spine determinism is *plausible but not yet established*
    for #179, so the headline-claim test is marked ``xfail(strict=False)``: it
    XFAILs (CI green) if determinism does not yet hold and XPASSes (still green) if
    it does — surfacing the signal without ever reddening CI. Live-LLM determinism
    is impossible and already lives as a soft eval metric (repeats=2 @graph-hash
    rate); we ship the stubbed-spine version as an observed-but-non-blocking signal
    now and promote it to a hard assert once the eval ``determinism_rate`` confirms
    it holds.
    """

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "spine determinism not yet established (#179); tracked as a soft eval "
            "metric (repeats=2 @graph-hash rate) until the eval determinism_rate "
            "confirms it, then promote to a hard assert"
        ),
    )
    def test_identical_graph_hash_across_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stable @graph hash (the A/B's exact signal) is identical run-to-run.

        With the bounded leaves pinned to fixed outputs, the goal is that the only
        nondeterminism in the §14 spine is removed — so two independent runs from
        fresh state assemble byte-identical crates.

        Hashing reuses :func:`eval.metrics.crate_graph_hash` — the *same* signal the
        A/B harness uses — so this test and the eval agree. That function already
        normalizes the known build-time volatility for us, so the comparison is
        meaningful rather than clock-noise:

        * it strips ``datePublished`` / ``dateModified`` (``_VOLATILE_NODE_KEYS``):
          ro-crate-py auto-stamps the root Dataset's ``datePublished`` with *today's*
          date, which would otherwise flip the hash across a midnight boundary even
          for two identical builds; and
        * it sorts every ``@graph`` node by ``@id`` and dumps with ``sort_keys``, so
          neither insertion order nor key order perturbs the hash.

        The spine's own ``@id``s are all deterministic (derived from entity names or
        verbatim AOP-Wiki IRIs — no UUIDs), so no extra ``@id`` normalization is
        needed here; reusing the harness hashing is sufficient and keeps the test
        and the A/B in lockstep.
        """
        from builder.agents.pipeline.pipeline import run_pipeline
        from eval.metrics import crate_graph_hash

        _stub_leaves(monkeypatch)
        e1 = _engine(_titled_state())
        run_pipeline(e1)
        h1 = crate_graph_hash(e1.state)

        # Fresh engine + fresh state; the stubs are still installed (same monkeypatch
        # scope) so the leaf outputs are identical to run 1.
        e2 = _engine(_titled_state())
        run_pipeline(e2)
        h2 = crate_graph_hash(e2.state)

        assert h1 == h2, "deterministic spine must build a byte-stable @graph"

    def test_graph_hash_is_a_stable_sha256(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity (BLOCKING): the determinism signal is a real 64-char hex digest.

        This is a hard assert because it does not depend on determinism *holding* —
        it only checks the hashing machinery produces a well-formed SHA-256 over a
        successfully built crate, so it stays green regardless of whether
        run-to-run stability is yet established.
        """
        from builder.agents.pipeline.pipeline import run_pipeline
        from eval.metrics import crate_graph_hash

        _stub_leaves(monkeypatch)
        engine = _engine(_titled_state())
        run_pipeline(engine)
        digest = crate_graph_hash(engine.state)

        assert isinstance(digest, str)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


# ---------------------------------------------------------------------------
# 4: no-op safety — no LLM provider configured
# ---------------------------------------------------------------------------


class TestPipelineE2ENoProviderSafety:
    """With no LLM provider, the spine still reaches base/isa/tox via scaffold."""

    def test_no_provider_reaches_conformance_via_scaffold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No provider ⇒ leaves are strict no-ops, scaffold path still conforms.

        The materialize-plan and drafter-leaf steps short-circuit when
        ``get_provider()`` returns ``None``; the deterministic scaffold + fix path
        alone must still build a {base, isa, tox}-conformant crate without crashing.
        The leaves are wired to raise if ever called, proving the no-op gate holds.
        """
        import builder.agents.pipeline.pipeline as pipeline_mod
        from builder.agents.pipeline.pipeline import run_pipeline

        # No provider configured.
        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: None)

        # The leaves must NEVER run when no provider is configured.
        def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("a bounded leaf ran without a provider configured")

        monkeypatch.setattr(pipeline_mod, "extract_plan", boom)
        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", boom)

        # A titled crate (would otherwise satisfy the context gate) to prove the
        # provider gate — not the context gate — is what makes it a no-op here.
        engine = _engine(_titled_state())
        result = run_pipeline(engine)

        assert result["conformance"] == {"base": True, "isa": True, "tox": True}
        assert result["ok"] is True
        # The scaffold path still laid the ISA backbone.
        assert {"Investigation", "Study", "Assay"} <= {
            e.type for e in engine.state.list_entities()
        }


# ---------------------------------------------------------------------------
# 5: extraction-context fidelity — bodies of rich files drive the plan (#231)
# ---------------------------------------------------------------------------


class TestExtractionContextFidelity:
    """`_gather_context` now reads non-tabular rich file BODIES, so a study-specific
    title in a ``.json`` / ``.docx`` reaches the extraction leaf and the
    materialized Study gets a real name — NOT the literal default ``"Study"`` (#231).

    Before #231 the spine fed the bounded extraction leaf only filenames + tiny
    tabular previews, so ``extract_plan`` saw nothing and returned an empty plan,
    and the backbone fell back to the literal ``"Study"`` default. The guard uses a
    stub ``extract_plan`` that echoes a study title ONLY when the context actually
    carries the document body marker — so a passing test proves the BODY made it
    into the context.
    """

    _BODY_MARKER = "TPO-INHIBITION-SVHPS26"

    def test_materialized_study_name_is_not_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A study title carried in a ``.json`` body materializes a real Study name.

        The stub ``extract_plan`` echoes a study name ONLY when the gathered context
        contains the document body marker (which lives in the ``.json`` BODY, never
        the filename). Running ``_materialize_plan`` on a backbone-free engine then
        creates the Study from the plan — proving the body reached the leaf and the
        Study is named from it, not the literal ``"Study"`` default.
        """
        import builder.agents.pipeline.pipeline as pipeline_mod
        from builder.state import FileClassification

        # Provider configured (no real model) so the leaf runs.
        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: "openai")

        study_name = "Methimazole TPO inhibition dose-response study"

        def fake_extract_plan(
            context: str, *, model: str | None = None, usage_sink: Any = None
        ) -> dict[str, Any]:
            # Echo a study name ONLY when the document BODY made it into context.
            if self._BODY_MARKER in context:
                return {"study": {"name": study_name}}
            return {}

        monkeypatch.setattr(pipeline_mod, "extract_plan", fake_extract_plan)

        # A non-tabular rich file whose BODY (not its filename) carries the marker.
        body_file = tmp_path / "S-VHPS26.json"
        body_file.write_text(
            f'{{"studyTitle": "{self._BODY_MARKER}", "organism": "Rattus"}}',
            encoding="utf-8",
        )
        fc = FileClassification(
            path=str(body_file),
            filename=body_file.name,
            size=body_file.stat().st_size,
            mime_type="application/json",
            first_rows=None,  # non-tabular: the scanner captured no preview
        )

        state = CrateState()  # NO title — the ONLY signal is the file body.
        state.approved_scan_roots.add(str(tmp_path.resolve()))
        state.scanned_files = [fc]
        engine = _engine(state)

        # Follow the real run_pipeline order: scaffold the ISA backbone, then
        # materialize the plan, which merges the (body-derived) Study name onto
        # the scaffolded Study (fill-don't-clobber over the generic default).
        pipeline_mod._scaffold_backbone(engine)
        pipeline_mod._materialize_plan(engine)

        study = next(
            (e for e in engine.state.list_entities() if e.type == "Study"), None
        )
        assert study is not None, "the body-derived plan must materialize a Study"
        name = str(study.fields.get("name") or "")
        assert name == study_name
        # The headline guard: NOT the literal default.
        assert name != "Study"

    def test_no_body_yields_no_study_specific_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: with no readable body, the leaf echoes nothing, so no Study is
        materialized from a plan (the spine would fall back to the default)."""
        import builder.agents.pipeline.pipeline as pipeline_mod
        from builder.state import FileClassification

        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: "openai")

        def fake_extract_plan(
            context: str, *, model: str | None = None, usage_sink: Any = None
        ) -> dict[str, Any]:
            if self._BODY_MARKER in context:  # pragma: no cover - must not fire
                return {"study": {"name": "should-not-happen"}}
            return {}

        monkeypatch.setattr(pipeline_mod, "extract_plan", fake_extract_plan)

        # A binary file with no first_rows whose body reader returns None.
        blob = tmp_path / "data.pzfx"
        blob.write_bytes(b"\x00\x01binary\x00not-text\x00")
        fc = FileClassification(
            path=str(blob),
            filename=blob.name,
            size=blob.stat().st_size,
            mime_type="application/octet-stream",
            first_rows=None,
        )
        state = CrateState()
        state.approved_scan_roots.add(str(tmp_path.resolve()))
        state.scanned_files = [fc]
        engine = _engine(state)

        result = pipeline_mod._materialize_plan(engine)
        # No body marker reached the leaf, so it echoed no study name.
        assert result["study"] == 0


class TestProcessParametersReachTheGraph:
    """Plan parameters must survive all the way to the exported ParameterValues (#379).

    Asserted on the BUILT ``@graph``, never on ``CrateState`` — the placeholders
    are minted during assembly by ``_crate_mapping._build_process`` and
    ``profiles/models/tox.py``, so a state-level assertion would not prove the
    value travelled schema -> merge -> draft_process -> _build_process -> model.
    """

    @staticmethod
    def _parameter_values(state) -> dict[str, str]:
        """Every ``PropertyValue`` in the assembled crate, by display name."""
        from builder.tools.builder import assemble_crate

        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
        graph = crate.metadata.generate().get("@graph", [])
        out: dict[str, str] = {}
        for node in graph:
            if not isinstance(node, dict) or "PropertyValue" not in str(node.get("@type")):
                continue
            name, value = node.get("name"), node.get("value")
            if isinstance(name, str) and isinstance(value, str):
                out[name] = value
        return out

    def test_exposure_parameter_value_carries_the_plan_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from builder.agents.pipeline.pipeline import run_pipeline

        _stub_leaves(monkeypatch)
        engine = _engine(_titled_state())
        run_pipeline(engine)

        assert self._parameter_values(engine.state)["Exposure Duration"] == "30 minutes"

    def test_exposure_duration_is_unknown_without_a_plan_parameter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HONESTY CONTROL — the value travelled, it was not coincidentally present.

        Identical run with the ``parameters`` key stripped from the plan: the
        placeholder returns. If this stayed "30 minutes" the test above would be
        asserting something the fixture supplied by another route.
        """
        import copy

        import builder.agents.pipeline.pipeline as pipeline_mod
        from builder.agents.pipeline.pipeline import run_pipeline

        _stub_leaves(monkeypatch)
        stripped = copy.deepcopy(_PLAN)
        for step in stripped["process_chain"]:
            step.pop("parameters", None)
        monkeypatch.setattr(
            pipeline_mod, "extract_plan", lambda *a, **k: copy.deepcopy(stripped)
        )

        engine = _engine(_titled_state())
        run_pipeline(engine)

        assert self._parameter_values(engine.state)["Exposure Duration"] == "unknown"

    def test_endpoint_readout_parameters_carry_plan_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Including `technical_replicate`, which only works if the shared
        `ENTITY_DRAFT_SCHEMA` addition landed — it was read but never advertised."""
        from builder.agents.pipeline.pipeline import run_pipeline

        _stub_leaves(monkeypatch)
        engine = _engine(_titled_state())
        run_pipeline(engine)

        values = self._parameter_values(engine.state)
        assert values["Detection Instrument"] == "Wizard2 gamma counter"
        assert values["Instrument Manufacturer"] == "Perkin Elmer"
        assert values["Endpoint"] == "TPO activity"
        assert values["Technical replicate"] == "3"

    def test_crate_still_conforms_with_parameters_supplied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards a regression that DROPS a ParameterValue instead of filling it."""
        from builder.agents.pipeline.pipeline import run_pipeline

        _stub_leaves(monkeypatch)
        result = run_pipeline(_engine(_titled_state()))

        assert result["conformance"] == {"base": True, "isa": True, "tox": True}
