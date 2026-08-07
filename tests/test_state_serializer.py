"""Tests for StateSerializer extraction from CrateState."""

from __future__ import annotations

import json

from builder.state import (
    CrateMetadata,
    CrateState,
    Entity,
    FileClassification,
    StateSerializer,
)


def _populated_state() -> CrateState:
    """Build a CrateState exercising every serialized field."""
    state = CrateState(
        session_id="sess-1",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
        metadata=CrateMetadata(title="Demo", description="A demo crate"),
    )
    state.add_entity(Entity(entity_id="inv-1", type="Investigation", fields={"name": "Inv"}))
    state.add_entity(Entity(entity_id="mol-1", type="MolecularEntity", fields={"name": "Mol"}))
    state.scanned_files.append(
        FileClassification(path="/data/a.csv", filename="a.csv", size=10, mime_type="text/csv")
    )
    state.approved_scan_roots.add("/data")
    state.validation.base_passed = True
    state.validation.required_issues.append("missing title")
    state.mit_assessment.overall_score = 0.5
    state.fair_assessment.dsm_level = 3
    state.checkpoint.next_actions.append("build_crate")
    state.iteration_count = 4
    state.max_iterations = 9
    state.log_reasoning("scan", "scan_files", "found 1 file")
    state.mark_stuck("needs review")
    return state


class TestStateSerializerOutput:
    """StateSerializer reproduces the documented serialization format."""

    def test_to_dict_has_expected_top_level_keys(self):
        state = CrateState()
        data = StateSerializer.to_dict(state)
        assert set(data) == {
            "session_id",
            "created_at",
            "updated_at",
            "metadata",
            "entities",
            "approved_scan_roots",
            "scanned_files",
            "documents",
            "document_evidence",
            "validation",
            "mit_assessment",
            "fair_assessment",
            "checkpoint",
            "validation_preferences",
            "user_answers",
            "generator",
            "iteration_count",
            "max_iterations",
            "stuck",
        }

    def test_to_dict_serializes_nested_components(self):
        data = StateSerializer.to_dict(_populated_state())
        assert data["session_id"] == "sess-1"
        assert data["metadata"]["title"] == "Demo"
        assert {e["entity_id"] for e in data["entities"]["investigations"]} == {"inv-1"}
        assert data["scanned_files"][0]["filename"] == "a.csv"
        assert data["approved_scan_roots"] == ["/data"]
        assert data["validation"]["base_passed"] is True
        assert data["iteration_count"] == 4
        assert data["max_iterations"] == 9
        assert data["stuck"] is True
        assert data["checkpoint"]["next_actions"] == ["build_crate"]


class TestStateSerializerRoundTrip:
    """from_dict(to_dict(state)) reconstructs an equivalent state."""

    def test_empty_state_round_trips(self):
        state = CrateState()
        restored = StateSerializer.from_dict(StateSerializer.to_dict(state))
        assert StateSerializer.to_dict(restored) == StateSerializer.to_dict(state)

    def test_populated_state_round_trips(self):
        state = _populated_state()
        restored = StateSerializer.from_dict(StateSerializer.to_dict(state))
        assert StateSerializer.to_dict(restored) == StateSerializer.to_dict(state)
        assert restored.get_entity("inv-1") is not None
        assert restored.iteration_count == 4
        assert restored.stuck is True

    def test_json_round_trips(self):
        state = _populated_state()
        restored = StateSerializer.from_json(StateSerializer.to_json(state))
        assert StateSerializer.to_dict(restored) == StateSerializer.to_dict(state)

    def test_to_json_is_valid_json(self):
        text = StateSerializer.to_json(_populated_state())
        assert json.loads(text)["session_id"] == "sess-1"

    def test_validation_preferences_round_trip(self):
        """The standing "don't ask me again" answers survive a save/resume.

        The existing round-trips compare ``to_dict`` output on states that leave
        this dict empty, so ``{} == {}`` held even while ``from_dict`` dropped
        the field entirely — the answers silently reset on every resume, which
        is precisely what persisting them exists to prevent.
        """
        state = CrateState()
        state.validation_preferences = {"recommended": True, "optional": False}
        restored = StateSerializer.from_dict(StateSerializer.to_dict(state))
        assert restored.validation_preferences == {"recommended": True, "optional": False}

    def test_partial_state_round_trips(self):
        state = CrateState(session_id="only-id")
        state.iteration_count = 2
        restored = StateSerializer.from_dict(StateSerializer.to_dict(state))
        assert restored.session_id == "only-id"
        assert restored.iteration_count == 2
        assert restored.list_entities() == []


class TestRegisterSerializer:
    """register_serializer adds/overrides component encoders without edits."""

    def test_registered_encoder_overrides_component_serialization(self):
        original = dict(StateSerializer._encoders)
        try:
            StateSerializer.register_serializer(CrateMetadata, lambda m: {"sentinel": True})
            data = StateSerializer.to_dict(_populated_state())
            assert data["metadata"] == {"sentinel": True}
        finally:
            StateSerializer._encoders = original

    def test_registered_encoder_handles_type_without_to_dict(self):
        class Custom:
            pass

        original = dict(StateSerializer._encoders)
        try:
            StateSerializer.register_serializer(Custom, lambda c: "encoded")
            assert StateSerializer._encode(Custom()) == "encoded"
        finally:
            StateSerializer._encoders = original


class TestCrateStateDelegation:
    """CrateState's serialization methods delegate to StateSerializer."""

    def test_crate_state_to_dict_delegates(self):
        state = _populated_state()
        assert state.to_dict() == StateSerializer.to_dict(state)

    def test_crate_state_from_dict_delegates(self):
        state = _populated_state()
        from_state = CrateState.from_dict(state.to_dict())
        from_serializer = StateSerializer.from_dict(state.to_dict())
        assert from_state.to_dict() == from_serializer.to_dict()

    def test_crate_state_json_round_trip_unchanged(self):
        state = _populated_state()
        restored = CrateState.from_json(state.to_json())
        assert restored.to_dict() == state.to_dict()


class TestExportStamp:
    """``export_crate`` records WHERE and WHEN, and neither disturbs validation."""

    def test_exported_at_round_trips(self):
        state = CrateState()
        state.metadata.output_path = "/data/crate"
        state.metadata.exported_at = "2026-08-07T11:42:31+02:00"
        restored = StateSerializer.from_dict(StateSerializer.to_dict(state))
        assert restored.metadata.exported_at == "2026-08-07T11:42:31+02:00"
        assert restored.metadata.output_path == "/data/crate"

    def test_absent_stamp_stays_absent(self):
        """A session that never exported must not gain a spurious key.

        The dashboard tells "never exported" from "exported before stamping"
        by presence, so an empty string or a default would erase that.
        """
        data = StateSerializer.to_dict(CrateState())
        assert "exported_at" not in data["metadata"]
        assert StateSerializer.from_dict(data).metadata.exported_at is None

    def test_stamp_does_not_move_the_validation_fingerprint(self):
        """Exporting must not invalidate a recorded verdict.

        ``exported_at`` lives in metadata, which the fingerprint hashes — and
        `export_crate` sets it on every write. Left in, the recorded verdict
        would read as stale immediately after every export and `ensure_validated`
        would re-run a full 3-pass SHACL sweep. Same reason `output_path` is
        excluded.
        """
        state = CrateState()
        state.metadata.title = "Crate"
        before = state.validation_fingerprint()

        state.metadata.output_path = "/data/crate"
        state.metadata.exported_at = "2026-08-07T11:42:31+02:00"
        assert state.validation_fingerprint() == before

        # Control: real metadata still moves it, so the exclusion is not blanket.
        state.metadata.title = "Renamed"
        assert state.validation_fingerprint() != before
