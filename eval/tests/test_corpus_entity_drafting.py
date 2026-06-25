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
