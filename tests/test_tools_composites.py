"""Tests for composite drafter tools (Issue #154).

``scaffold_isa_backbone`` fuses the recurring draft_investigation ->
draft_study -> draft_assay sequence into one deterministic, pure call so a weak
model reaches a BASE-passing backbone in a single tool call instead of 3-4
round-trips (and stops thrashing on cross-turn id-threading). It is idempotent:
re-running reuses the existing backbone rather than minting duplicates, and it
never invents File entities (FileClassification carries no role, so binding
would manufacture ISA-layer orphans).
"""

from __future__ import annotations

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.composites import scaffold_isa_backbone
from builder.tools.drafters import draft_investigation
from builder.tools.validation import build_and_validate


def _by_type(state: CrateState, type_name: str) -> list:
    return [e for e in state.list_entities() if e.type == type_name]


class TestScaffoldBackbone:
    def test_creates_linked_backbone(self):
        state = CrateState()
        result = scaffold_isa_backbone(state)

        inv = _by_type(state, "Investigation")
        study = _by_type(state, "Study")
        assay = _by_type(state, "Assay")
        assert len(inv) == len(study) == len(assay) == 1

        assert study[0].fields["investigation_id"] == inv[0].entity_id
        assert assay[0].fields["study_id"] == study[0].entity_id

        assert result["investigation_id"] == inv[0].entity_id
        assert result["study_id"] == study[0].entity_id
        assert result["assay_id"] == assay[0].entity_id

    def test_backbone_passes_base_validation(self):
        state = CrateState()
        scaffold_isa_backbone(state)
        report = build_and_validate(state, profile="base")
        assert report["conformance"]["base"] is True
        assert report["ok"] is True

    def test_idempotent_no_duplicates(self):
        state = CrateState()
        scaffold_isa_backbone(state)
        scaffold_isa_backbone(state)
        assert len(_by_type(state, "Investigation")) == 1
        assert len(_by_type(state, "Study")) == 1
        assert len(_by_type(state, "Assay")) == 1

    def test_reuses_existing_investigation(self):
        state = CrateState()
        existing = draft_investigation(state, {"name": "Pre-existing"})
        result = scaffold_isa_backbone(state)
        assert result["investigation_id"] == existing.entity_id
        assert len(_by_type(state, "Investigation")) == 1

    def test_applies_hints(self):
        state = CrateState()
        scaffold_isa_backbone(
            state,
            investigation={"name": "My Inv"},
            study={"name": "My Study"},
            assay={"name": "My Assay"},
        )
        assert _by_type(state, "Investigation")[0].fields["name"] == "My Inv"
        assert _by_type(state, "Study")[0].fields["name"] == "My Study"
        assert _by_type(state, "Assay")[0].fields["name"] == "My Assay"

    def test_creates_no_file_entities(self):
        state = CrateState()
        scaffold_isa_backbone(state)
        assert _by_type(state, "File") == []

    def test_optional_validate_returns_base_report(self):
        state = CrateState()
        result = scaffold_isa_backbone(state, validate_base=True)
        assert "validation" in result
        assert result["validation"]["conformance"]["base"] is True

    def test_no_validation_key_by_default(self):
        state = CrateState()
        result = scaffold_isa_backbone(state)
        assert "validation" not in result


class TestScaffoldViaEngine:
    def test_callable_through_run_tool(self):
        engine = AgentEngine()
        engine.initialize()
        result = engine.run_tool("scaffold_isa_backbone")
        assert result["investigation_id"]
        assert engine.state.get_entity(result["investigation_id"]) is not None
        assert engine.state.get_entity(result["assay_id"]) is not None
