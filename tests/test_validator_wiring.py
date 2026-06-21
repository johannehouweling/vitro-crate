"""Regression tests for the three-pass validator wiring (profiles/validator.py).

These guard the SHAPES_DIR path (which previously pointed outside the repo, so
the ISA-Tox pass silently failed to load its custom shapes) and assert that a
representative crate built by build_crate conforms across all three SHACL passes
at REQUIRED severity — the real end-to-end check.
"""

from __future__ import annotations

import warnings

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate
from profiles.validator import SHAPES_DIR, validate_crate


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def test_shapes_dir_resolves_inside_repo():
    """The ISA-Tox shapes directory must exist and hold the tox profile."""
    assert SHAPES_DIR.exists(), f"SHAPES_DIR does not exist: {SHAPES_DIR}"
    assert (SHAPES_DIR / "tox").is_dir()


def _representative_state() -> CrateState:
    state = CrateState()
    state.metadata.title = "Demo Investigation"
    state.metadata.description = "A representative ISA-Tox crate"
    state.metadata.accession = "S-VHPS99"
    state.add_entity(
        _ent(
            "study_1",
            "Study",
            name="Hepatotoxicity study",
            aop="https://aopwiki.org/aops/37",
        )
    )
    state.add_entity(
        _ent(
            "assay_1",
            "Assay",
            name="Viability assay",
            study_id="study_1",
            key_event="https://aopwiki.org/events/55",
        )
    )
    state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin A"))
    state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2", accession="CVCL_0027"))
    state.add_entity(
        _ent(
            "cc",
            "LabProcess",
            name="Cell Culture",
            process_type="CellCulture",
            assay_id="assay_1",
            cell_line="cell_1",
        )
    )
    state.add_entity(
        _ent(
            "exp",
            "LabProcess",
            name="Exposure",
            process_type="Exposure",
            assay_id="assay_1",
            samples="cell_1",
            chemicals="chem_1",
            duration="24h",
            microplate="96-well",
        )
    )
    return state


def test_representative_crate_passes_all_three_required(tmp_path):
    """A built crate conforms to base RO-Crate 1.1, ISA and ISA-Tox at REQUIRED."""
    out = tmp_path / "crate"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert build_crate(_representative_state(), str(out))["success"] is True
        results = validate_crate(out)

    assert len(results) == 3  # base RO-Crate, ISA, ISA-Tox
    for res in results:
        assert res.passed_required, f"{res.profile} has REQUIRED issues: {res.required_issues}"
