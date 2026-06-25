"""Tests for the realistic *arbitrary tox folder* corpus case (Issue #179).

The existing structured cases scan a thin README-plus-CSV folder. The decision
gate (epic #179, task 6) wants the A/B to exercise the full
``scan -> extract -> materialize -> assess`` path over a *realistic* arbitrary
research folder — a few raw documents a researcher actually keeps (a study
description, a methods/protocol write-up, a compound list, and the measurement
files) — so a good build must draft the complete in-vitro tox chain, not just a
backbone.

These tests are structural and fully offline: they assert the new case loads, its
fixture files exist and are discoverable, its ``min_entities`` floor is set to a
defensible *complete-study* bar, and ``meets_entity_quota`` works for it. They do
**not** run a live LLM or the network. The conformance-touching build_state test
carries the harness 120s timeout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.corpus import (
    DEFAULT_CORPUS,
    EvalCase,
    meets_entity_quota,
    reaches_isa_tox_conformance,
)

CASE_ID = "arbitrary-tox-folder"


def _case() -> EvalCase:
    match = [c for c in DEFAULT_CORPUS if c.case_id == CASE_ID]
    assert match, f"expected an {CASE_ID!r} case in the corpus"
    return match[0]


class TestArbitraryToxFolderCaseRegistered:
    """The realistic arbitrary-folder case is present and well-formed."""

    def test_case_is_in_the_corpus(self) -> None:
        case = _case()
        # An arbitrary research folder with no metadata file is the unstructured
        # tier: the whole crate must be elicited from raw docs the agent scans.
        assert case.kind == "unstructured"
        assert case.prompt, "the arbitrary-folder case must carry a prompt"

    def test_case_scans_a_folder_so_the_full_path_runs(self) -> None:
        # The case feeds its input by pointing input_path at a folder, so the
        # harness runs scan -> extract -> materialize -> assess for BOTH archs.
        case = _case()
        assert case.input_path is not None, (
            "the case must scan a folder (input_path) so the build runs the full "
            "scan->extract->materialize path, not a pre-seeded backbone"
        )
        root = Path(case.input_path)
        assert root.is_dir(), f"{case.input_path} must be an in-repo directory"

    def test_fixture_ships_realistic_arbitrary_research_docs(self) -> None:
        case = _case()
        root = Path(case.input_path or "")
        # A realistic folder: a study description, a methods/protocol doc, a
        # compound list, and at least one measurements + one results file.
        assert (root / "study_description.md").is_file()
        assert (root / "methods_protocol.txt").is_file()
        assert (root / "compounds.csv").is_file()

    def test_fixture_files_are_discoverable_by_a_recursive_scan(self) -> None:
        # The scanner walks the folder recursively; the data files live in
        # subdirectories, so a recursive discovery must find them.
        case = _case()
        root = Path(case.input_path or "")
        csvs = [p for p in root.rglob("*.csv") if p.is_file()]
        assert len(csvs) >= 2, "expected at least a measurements and a results CSV"
        # The measurement/analysis files are nested, proving recursion matters.
        nested = [p for p in csvs if p.parent != root]
        assert nested, "at least one data file should be in a subdirectory"

    def test_case_declares_a_complete_study_entity_quota(self) -> None:
        case = _case()
        assert case.min_entities is not None, (
            "the arbitrary-folder case must declare min_entities so the A/B "
            "measures whether the build drafted a COMPLETE study"
        )
        q = case.min_entities
        # A complete in-vitro tox study (profiles/docs/isa_tox.md): the ISA
        # backbone, a cell line, a compound, a protocol, the four-step process
        # chain, and the raw + processed data files.
        assert q.get("Investigation", 0) >= 1
        assert q.get("Study", 0) >= 1
        assert q.get("Assay", 0) >= 1
        assert q.get("CellLineSample", 0) >= 1
        assert q.get("MolecularEntity", 0) >= 1
        assert q.get("LabProtocol", 0) >= 1
        # The full derivation chain is four LabProcess steps (CellCulture ->
        # Exposure -> EndpointReadout -> DataAnalysis).
        assert q.get("LabProcess", 0) >= 4
        # Raw + processed data files attached.
        assert q.get("File", 0) >= 2


class TestArbitraryToxFolderQuota:
    """The quota floor behaves as a pure content-quality check for this case."""

    def test_empty_backbone_misses_the_complete_study_quota(self) -> None:
        from builder.state import CrateState, Entity, EntityProvenance, EntityType

        def _ent(eid: str, t: EntityType, **f: object) -> Entity:
            return Entity(
                entity_id=eid,
                type=t,
                fields=f,
                _provenance=EntityProvenance(created_by="llm"),
            )

        # A conformant-shaped backbone with a single Exposure (what a thin build
        # produces) must FALL SHORT of the complete-study quota — that is the
        # whole point of demanding the full chain.
        state = CrateState()
        state.add_entity(_ent("inv", "Investigation", name="x"))
        state.add_entity(_ent("study", "Study", name="x"))
        state.add_entity(_ent("assay", "Assay", name="x"))
        state.add_entity(_ent("cell", "CellLineSample", name="c"))
        state.add_entity(_ent("compound", "MolecularEntity", name="m"))
        state.add_entity(
            _ent("exp", "LabProcess", name="e", process_type="Exposure")
        )

        result = meets_entity_quota(state, _case().min_entities)
        assert result["meets_quota"] is False
        # It is short the protocol, three of the four process steps, and the files.
        assert result["missing"].get("LabProtocol", 0) >= 1
        assert result["missing"].get("LabProcess", 0) >= 3
        assert result["missing"].get("File", 0) >= 2


@pytest.mark.timeout(120)
class TestArbitraryToxFolderBuildState:
    """The case's mock build_state both conforms AND meets its own quota.

    This is the offline stand-in a *good* agent would produce from the folder: it
    must satisfy BOTH the strict ``{base, isa, tox}`` predicate and the complete-
    study quota, so the quality signal is exercisable end to end without a model.
    """

    def test_build_state_reaches_conformance_and_meets_quota(self) -> None:
        case = _case()
        assert case.build_state is not None
        state = case.build_state()

        predicate = reaches_isa_tox_conformance(state)
        assert predicate["success"] is True, predicate["issues"]
        assert predicate["conformance"] == {"base": True, "isa": True, "tox": True}

        quota = meets_entity_quota(state, case.min_entities)
        assert quota["meets_quota"] is True, quota["missing"]
