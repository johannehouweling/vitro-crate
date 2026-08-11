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
from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.composites import _is_consumed_by_process, scaffold_isa_backbone
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


class TestScaffoldMergesHintsIntoReused:
    """Idempotent-with-merge (#232): re-scaffolding a reused entity must FILL its
    empty fields from the supplied hints rather than dropping them, while never
    clobbering a value the entity already carries (fill-don't-clobber)."""

    def test_fills_empty_field_on_reused_entity(self):
        state = CrateState()
        # An existing Study with NO name (a field a later hint should fill).
        scaffold_isa_backbone(state)
        study = _by_type(state, "Study")[0]
        study.fields.pop("name", None)

        result = scaffold_isa_backbone(state, study={"name": "Filled name"})

        assert "Study" in result["reused"]  # still reused, not duplicated
        assert len(_by_type(state, "Study")) == 1
        assert _by_type(state, "Study")[0].fields.get("name") == "Filled name"

    def test_does_not_clobber_existing_value_on_reused_entity(self):
        state = CrateState()
        scaffold_isa_backbone(state, study={"name": "Original name"})

        # A second call with a different hint must NOT overwrite the real value.
        scaffold_isa_backbone(state, study={"name": "Different name"})

        assert len(_by_type(state, "Study")) == 1
        assert _by_type(state, "Study")[0].fields.get("name") == "Original name"

    def test_merge_adds_a_new_field_without_touching_others(self):
        state = CrateState()
        scaffold_isa_backbone(state, study={"name": "Keep me"})

        # Supply a description (previously absent) plus a conflicting name.
        scaffold_isa_backbone(
            state, study={"name": "Ignore me", "description": "New desc"}
        )

        study = _by_type(state, "Study")[0]
        assert study.fields.get("name") == "Keep me"  # existing value preserved
        assert study.fields.get("description") == "New desc"  # empty field filled


def _ent(entity_id: str, type_: EntityType, **fields) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


class TestConsumedByProcessCountsOnlyBuildReadFields:
    """The orphan backstop must agree with what assembly actually reads.

    A compound sitting under a process's `input` is read by nothing — the ISA
    shape allows only File/Sample/BioSample there, so `_build_process` takes
    compounds from `chemicals`. Counting `input` as "consumed" made this check
    pass for the exact entity that would go missing from the exported crate.
    """

    def _state(self):
        state = CrateState()
        state.add_entity(_ent("cmp", "MolecularEntity", name="doxorubicin"))
        return state

    def test_compound_only_under_input_is_not_consumed(self):
        state = self._state()
        state.add_entity(_ent("exp", "LabProcess", process_type="Exposure", input="cmp"))
        assert _is_consumed_by_process(state, "cmp") is False

    def test_compound_under_chemicals_is_consumed(self):
        state = self._state()
        state.add_entity(
            _ent("exp", "LabProcess", process_type="Exposure", chemicals="cmp")
        )
        assert _is_consumed_by_process(state, "cmp") is True

    def test_a_file_still_counts_through_the_ordinary_io_fields(self):
        # Only types with a declared build home are narrowed; everything else
        # keeps the full set of process I/O fields.
        state = CrateState()
        state.add_entity(_ent("f1", "File", name="a.csv"))
        state.add_entity(
            _ent("p", "LabProcess", process_type="DataAnalysis", object="f1")
        )
        assert _is_consumed_by_process(state, "f1") is True
