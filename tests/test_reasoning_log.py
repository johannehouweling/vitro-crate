"""Tests for ReasoningLog extraction from CrateState."""

from __future__ import annotations

from builder.state import CrateState, ReasoningLog


class TestReasoningLog:
    """Test suite for ReasoningLog behavior."""

    def test_log_reasoning_appends_sequential_steps(self):
        """log_reasoning appends steps with incrementing numbers."""
        log = ReasoningLog()

        first = log.log_reasoning("initialize", "scan_files", "Scanned 1 file")
        second = log.log_reasoning("build", "build_crate", "Built partial crate")

        assert first.step == 1
        assert second.step == 2
        assert [step.action for step in log.reasoning_log] == ["initialize", "build"]

    def test_mark_stuck_sets_flag_and_logs_reason(self):
        """mark_stuck sets the stuck flag and records the reason."""
        log = ReasoningLog()

        step = log.mark_stuck("Cannot resolve identifier")

        assert log.stuck is True
        assert step.action == "mark_stuck"
        assert step.result == "Cannot resolve identifier"
        assert log.reasoning_log[-1] == step

    def test_is_stuck_returns_true_at_iteration_limit(self):
        """is_stuck returns True once the iteration limit is reached."""
        log = ReasoningLog(iteration_count=2, max_iterations=2)

        assert log.is_stuck() is True


class TestCrateStateReasoningDelegation:
    """Test suite for CrateState delegation to ReasoningLog."""

    def test_crate_state_delegates_reasoning_state(self):
        """CrateState delegates iteration and stuck state to checkpoint."""
        state = CrateState()

        state.iteration_count += 1
        state.max_iterations = 3
        step = state.log_reasoning("draft", "draft_investigation", "Created entity")
        state.mark_stuck("Need user input")

        assert isinstance(state.checkpoint, ReasoningLog)
        assert state.iteration_count == 1
        assert state.max_iterations == 3
        assert state.stuck is True
        assert step.step == 1
        assert state.checkpoint.reasoning_log[-1].result == "Need user input"

    def test_crate_state_serialization_preserves_reasoning_log_fields(self):
        """CrateState round-trips delegated reasoning fields via serialization."""
        state = CrateState()
        state.checkpoint.next_actions.append("build_crate")
        state.checkpoint.completed_checkpoints.append("files_scanned")
        state.iteration_count = 4
        state.max_iterations = 7
        state.log_reasoning("validate", "validate", "Validation passed")
        state.mark_stuck("Waiting for review")

        data = state.to_dict()

        assert data["checkpoint"]["next_actions"] == ["build_crate"]
        assert data["checkpoint"]["completed_checkpoints"] == ["files_scanned"]
        assert len(data["checkpoint"]["reasoning_log"]) == 2
        assert data["iteration_count"] == 4
        assert data["max_iterations"] == 7
        assert data["stuck"] is True

        restored = CrateState.from_dict(data)

        assert isinstance(restored.checkpoint, ReasoningLog)
        assert restored.checkpoint.next_actions == ["build_crate"]
        assert restored.checkpoint.completed_checkpoints == ["files_scanned"]
        assert restored.iteration_count == 4
        assert restored.max_iterations == 7
        assert restored.stuck is True
        assert [step.action for step in restored.checkpoint.reasoning_log] == [
            "validate",
            "mark_stuck",
        ]
