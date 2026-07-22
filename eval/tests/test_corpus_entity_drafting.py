"""Tests for the entity-drafting corpus case + its content-quality metric.

The original corpus measures *reliability of acting* — its success predicate is
purely ``{base, isa, tox}`` conformance, which an empty-ish backbone can reach.
This adds one richer structured case (Issue #179) designed so an agent must draft
several distinct domain entities, plus an **additive** content-quality signal
(``min_entities`` on the case + ``meets_quota`` on the result) that measures
whether the drafted *content* is there — not just that the agent acted.

These tests are offline. The conformance-touching test carries the harness 120s
timeout; the corpus/metric-shape tests are pure and fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.corpus import (
    DEFAULT_CORPUS,
    EvalCase,
    meets_entity_quota,
)


class TestEntityDraftingCaseRegistered:
    """The richer entity-drafting case is present and well-formed."""

    CASE_ID = "structured-svhps22"

    def _case(self) -> EvalCase:
        match = [c for c in DEFAULT_CORPUS if c.case_id == self.CASE_ID]
        assert match, f"expected a {self.CASE_ID!r} case in the corpus"
        return match[0]

    def test_case_is_in_the_corpus(self) -> None:
        case = self._case()
        assert case.kind == "structured"
        assert case.prompt, "the entity-drafting case must carry a prompt"

    def test_case_points_at_an_existing_offline_fixture(self) -> None:
        case = self._case()
        assert case.input_path is not None
        root = Path(case.input_path)
        assert root.is_dir(), f"{case.input_path} must be an in-repo directory"
        # A README plus at least two data files — enough that an agent must draft
        # several entities (compound, cell line, files, backbone).
        assert (root / "README.md").is_file()
        data_files = [p for p in root.rglob("*.csv") if p.is_file()]
        assert len(data_files) >= 2, "fixture should ship a couple of data files"

    def test_case_declares_a_minimum_entity_quota(self) -> None:
        case = self._case()
        assert case.min_entities is not None, (
            "the entity-drafting case must declare min_entities so the A/B can "
            "measure draft quality, not just that the agent acted"
        )
        # The quota must demand the domain content that distinguishes a real draft
        # from an empty backbone: a compound, a cell line, and at least one file.
        assert case.min_entities.get("MolecularEntity", 0) >= 1
        assert case.min_entities.get("CellLineSample", 0) >= 1
        assert case.min_entities.get("File", 0) >= 2


class TestMeetsEntityQuota:
    """``meets_entity_quota`` is a pure content-quality check over a state."""

    def test_none_quota_is_undefined_quality(self) -> None:
        from builder.state import CrateState

        result = meets_entity_quota(CrateState(), None)
        # No quota declared -> quality is not assessed (None), counts still given.
        assert result["meets_quota"] is None
        assert result["entity_counts"] == {}
        assert result["missing"] == {}

    def test_empty_state_misses_a_real_quota(self) -> None:
        from builder.state import CrateState

        quota = {"MolecularEntity": 1, "CellLineSample": 1, "File": 2}
        result = meets_entity_quota(CrateState(), quota)
        assert result["meets_quota"] is False
        # Everything is missing, by exactly the demanded amount.
        assert result["missing"] == {"MolecularEntity": 1, "CellLineSample": 1, "File": 2}

    def test_state_meeting_quota_passes(self) -> None:
        from builder.state import CrateState, Entity, EntityProvenance, EntityType

        def _ent(eid: str, t: EntityType, **f: object) -> Entity:
            return Entity(
                entity_id=eid,
                type=t,
                fields=f,
                _provenance=EntityProvenance(created_by="llm"),
            )

        state = CrateState()
        state.add_entity(_ent("c", "MolecularEntity", name="Methimazole"))
        state.add_entity(_ent("cell", "CellLineSample", name="FRTL-5"))
        state.add_entity(_ent("f1", "File", name="raw.csv", path="raw.csv"))
        state.add_entity(_ent("f2", "File", name="ic50.csv", path="ic50.csv"))

        quota = {"MolecularEntity": 1, "CellLineSample": 1, "File": 2}
        result = meets_entity_quota(state, quota)
        assert result["meets_quota"] is True
        assert result["missing"] == {}
        assert result["entity_counts"]["File"] == 2
        assert result["entity_counts"]["MolecularEntity"] == 1


@pytest.mark.timeout(120)
class TestEntityDraftingCaseBuildState:
    """The case's mock build_state both conforms AND meets its own quota.

    This is the offline stand-in a *good* agent would produce: it must satisfy
    BOTH the strict ``{base, isa, tox}`` predicate and the content quota, so the
    quality signal is exercised end to end without a live model.
    """

    def test_build_state_reaches_conformance_and_meets_quota(self) -> None:
        from eval.corpus import reaches_isa_tox_conformance

        case = next(c for c in DEFAULT_CORPUS if c.case_id == "structured-svhps22")
        assert case.build_state is not None
        state = case.build_state()

        predicate = reaches_isa_tox_conformance(state)
        assert predicate["success"] is True, predicate["issues"]
        assert predicate["conformance"] == {"base": True, "isa": True, "tox": True}

        quota = meets_entity_quota(state, case.min_entities)
        assert quota["meets_quota"] is True, quota["missing"]


class TestEntityDraftingCaseLinksAOP:
    """The svhps22 case exercises AOP-Wiki linking (Issue #180) end to end.

    TPO inhibition is a well-characterised Adverse Outcome Pathway (AOP-Wiki 42),
    so a *good* svhps22 build must draft the pathway as a typed
    ``AdverseOutcomePathway`` entity AND reference it from the Study
    (``schema:mentions``). Before this, no corpus case exercised AOP linking, so
    neither A/B arm was ever scored on getting it — the feature could silently
    regress. The quota demands the AOP so the harness now measures it.
    """

    CASE_ID = "structured-svhps22"
    AOP_IRI = "https://aopwiki.org/aops/42"

    def _case(self) -> EvalCase:
        return next(c for c in DEFAULT_CORPUS if c.case_id == self.CASE_ID)

    def test_quota_demands_an_adverse_outcome_pathway(self) -> None:
        case = self._case()
        assert case.min_entities is not None
        assert case.min_entities.get("AdverseOutcomePathway", 0) >= 1, (
            "the svhps22 quota must demand an AdverseOutcomePathway so the A/B "
            "measures AOP linking, not just backbone + compound + cell line"
        )

    def test_build_state_materializes_a_typed_aop(self) -> None:
        case = self._case()
        assert case.build_state is not None
        state = case.build_state()
        aops = state.list_entities(entity_type="AdverseOutcomePathway")
        assert len(aops) >= 1, (
            "the good svhps22 stand-in must draft an AdverseOutcomePathway entity"
        )

    def test_study_mentions_the_aop(self) -> None:
        case = self._case()
        assert case.build_state is not None
        state = case.build_state()
        studies = state.list_entities(entity_type="Study")
        assert studies, "expected a Study to hang the AOP mention off"
        ref_ids: set[str] = set()
        for study in studies:
            value = study.fields.get("aop")
            if not value:
                continue
            for ref in value if isinstance(value, list) else [value]:
                ref_ids.add(ref.get("@id") if isinstance(ref, dict) else ref)
        assert self.AOP_IRI in ref_ids, (
            f"the Study should mention the AOP via schema:mentions; got {ref_ids}"
        )
